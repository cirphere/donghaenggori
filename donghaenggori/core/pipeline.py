"""접수 파이프라인 오케스트레이션 — md 5-3 '전화 한 통이 접수카드가 되기까지' 10단계.

  ① 발화 입력(전화/앱·웹/직접)   ② STT(텍스트 입력으로 대체 가능)
  ③ 발신번호 → 케어 프로필       ④ 과거 이력·단골 병원
  ⑤ 발화 의도 분류               ⑥ 병원 후보·날짜 해석·동행 필요도
  ⑦ RAG 지역 복지자원 보강        ⑧ 접수카드 생성
  ─────────────── 여기까지 AI ───────────────
  ⑨ 사회복지사 확인·수정          ⑩ 사후 메모 → 요약 → 프로필 업데이트

의도 분류는 학습 모델(intent_model)을 우선 쓰고, 없으면 규칙 NLU로 폴백한다.
긴급 신호는 접수카드를 만들지 않고 즉시 사람에게 넘긴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import card as card_mod
from . import classify as classify_mod
from . import dateparse
from . import db
from .korean import josa, particle
from . import hospital as hospital_mod
from . import identity as identity_mod
from . import needlevel as need_mod
from . import nlu as nlu_mod

CHANNELS = ("전화", "앱·웹(보호자)", "직접(기관)")

# 분류 관련 상수는 분류기 쪽으로 옮겼다. 예전 이름으로 참조하던 곳이 있어 남겨둔다.
URGENT_CONFIDENT = classify_mod.URGENT_CONFIDENT
RULE_OWNED_INTENTS = classify_mod.RULE_OWNED_INTENTS

# 참고 후보(화면 04 4-A) 조회 조건. 예열도 반드시 같은 값을 써야 한다 —
# 공공 API 캐시 키에 파라미터가 들어가서, 반경이나 건수가 다르면 예열해도
# 실제 호출은 캐시를 못 타고 처음부터 다시 기다린다(시연 중 타임아웃 났다).
REFERENCE_RADIUS_M = 6000
REFERENCE_ROWS = 3


@dataclass
class Result:
    urgent: bool
    card: card_mod.Card | None
    analysis: nlu_mod.Analysis
    profile: dict | None
    channel: str = "전화"
    urgent_message: str | None = None
    urgent_confident: bool = True   # 긴급을 단정할 근거가 있는가
    intent_source: str = "규칙"          # 학습모델 | 규칙 | 규칙+LLM
    intent_confidence: float | None = None
    facilities: list[dict] = field(default_factory=list)   # ⑦ RAG 보강 결과

    def to_dict(self) -> dict:
        return {
            "urgent": self.urgent,
            "channel": self.channel,
            "intent": self.analysis.intent,
            "intent_source": self.intent_source,
            "intent_confidence": self.intent_confidence,
            "dept": self.analysis.dept,
            "symptom": self.analysis.symptom,
            "date": self.analysis.date,
            "profile": self.profile,
            "urgent_message": self.urgent_message,
            "urgent_confident": self.urgent_confident,
            "facilities": self.facilities,
            "card": self.card.to_dict() if self.card else None,
        }


def _classify(utterance: str, use_llm: bool | None,
              classifier: classify_mod.Classifier | None = None) -> classify_mod.Classification:
    """⑤ 의도 분류. 어떤 분류기를 쓰는지는 이 함수 바깥의 관심사다.

    결과는 쓰기 전에 검증한다 — 자세한 이유는 classify 모듈 설명 참조.
    계약을 어기면 규칙 사전으로 내려앉고, 그 사실을 노트에 남긴다.
    접수 자체를 못 하게 되는 것보다는 낫지만, 조용히 넘어가지도 않는다.
    """
    clf = classifier or classify_mod.default_classifier(use_llm)
    try:
        return classify_mod.validate(clf.classify(utterance))
    except classify_mod.ClassifierContractError as e:
        fallback = classify_mod.validate(
            classify_mod.RuleOnlyClassifier(use_llm=False).classify(utterance))
        fallback.analysis.notes.append(f"분류기 계약 위반으로 규칙 사전 사용: {e}")
        return fallback


def run(phone: str, utterance: str, channel: str = "전화",
        use_llm: bool | None = None, with_rag: bool = True,
        classifier: classify_mod.Classifier | None = None,
        identity_denied: bool = False) -> Result:
    """identity_denied — 발신자가 **번호 주인이 아니라고 직접 밝힌** 경우.

    전화에서 "박순자 님 맞으신가요"에 2번을 누른 상황이다. 그때도 번호 주인의
    프로필로 카드를 채우면, 필요도(장기요양등급)와 병원 추천이 **다른 사람 것**
    으로 붙는다. 카드에는 '확인 필요' 가 뜨지만 내용 자체가 남의 정보라, 복지사가
    그 표시를 놓치면 엉뚱한 기준으로 동행을 준비하게 된다.

    아니라고 밝혔으면 그 말을 따른다. 프로필을 카드에 쓰지 않고, 발화에서 얻은
    것만 남긴다. 대상자 확정은 원문을 듣고 복지사가 한다.
    """
    owner = db.get_profile(phone)                                  # ③④
    prof = None if identity_denied else owner
    c = _classify(utterance, use_llm, classifier)                  # ⑤
    a, source, conf, confident = c.analysis, c.source, c.confidence, c.urgent_confident

    if a.urgent:                                                   # 긴급 → 접수 중단
        msg = ("긴급 신호 감지 — 접수카드 생성을 중단하고 즉시 담당자·사람 상담으로 연결합니다. "
               "동행고리 AI는 응급 여부를 판단하지 않습니다.")
        if not confident:
            # 근거가 약하다. 사람에게 넘기는 것은 같지만 긴급이라고 단정하지 않는다.
            msg = ("발화를 명확히 이해하지 못했습니다 — 접수카드를 만들지 않고 담당자 확인으로 넘깁니다. "
                   "긴급 가능성을 배제하지 않았으므로 사람이 직접 확인해 주세요.")
        return Result(
            urgent=True, card=None, analysis=a, profile=prof, channel=channel,
            urgent_message=msg, urgent_confident=confident,
            intent_source=source, intent_confidence=conf)

    hres = hospital_mod.suggest(prof, a.dept, spoken=a.hospital)   # ⑥
    nres = need_mod.assess(prof)

    facilities: list[dict] = []
    if with_rag:                                                   # ⑦
        try:
            from ..services import rag
            facilities = rag.enrich(prof, a)
        except Exception:
            facilities = []

    c = _build_card(phone, utterance, a, prof, hres, nres, channel,
                    denied_owner=owner if identity_denied else None)  # ⑧
    c.outing_checklist = _outing_checklist(prof, a)
    if hres.status == "확인 필요" and not (prof or {}).get("history"):
        c.reference_candidates = _reference_candidates(prof, a)
    return Result(urgent=False, card=c, analysis=a, profile=prof, channel=channel,
                  intent_source=source, intent_confidence=conf, facilities=facilities)


def _reference_candidates(prof: dict | None, a) -> list[dict]:
    """화면 04 4-A — 이력이 없을 때 거리 기준 '참고 후보'.

    확정 후보가 아니다. 근거가 거리뿐임을 각 항목에 명시하고, 사회복지사가
    확인전화로 확정하도록 남겨둔다. 외부 API가 없으면 빈 목록으로 폴백한다.
    """
    if not prof:
        return []
    from . import geo
    latlon = geo.coords_of(prof.get("region"))
    if latlon is None:
        return []
    try:
        from ..services import hira
        res = hira.nearby(latlon[0], latlon[1], dept=a.dept,
                          radius_m=REFERENCE_RADIUS_M, rows=REFERENCE_ROWS)
    except Exception:
        return []
    if res.unavailable:
        return []
    precise = geo.is_precise(prof.get("region"))
    # 진료과 필터는 '해당 과목 보유 기관'을 뜻한다. 내과의원이 정형외과를 겸하는 등
    # 기관 이름과 과목이 달라 보일 수 있으므로 어떤 기준으로 걸렀는지 함께 표시한다.
    dept_note = f"{a.dept} 진료과목 보유" if a.dept else "진료과 미지정"
    out = []
    for h in res.data or []:
        out.append({
            "name": h["name"], "kind": h["kind"], "address": h["address"],
            "phone": h["phone"],
            "distance_m": h.get("distance_m"),
            "matched_by": dept_note,
            "basis": h["basis"] + ("" if precise else " (지역 대표 좌표 기준 근사)"),
            "source": h["source"],
        })
    return out


def _outing_checklist(prof: dict | None, a) -> list[str]:
    """외출 전 체크리스트 — 기상·대기 참고 정보.

    외부 API가 느리거나 미연동이면 조용히 건너뛴다. 접수 흐름을 막지 않는다.
    방문 가부는 판단하지 않고 참고 문구만 만든다.

    좌표는 병원 후보와 같은 geo.coords_of 를 쓴다. 예전에는 여기만 시도 대표
    좌표를 따로 들고 있었는데, 전남은 그 한 점이 목포 근처라 곡성 91km ·
    고흥 78km 떨어진 날씨를 보여줬다. 산간과 해안의 예보가 뒤바뀌는 거리다.
    """
    if not prof:
        return []
    from . import geo
    region = prof.get("region") or ""
    latlon = geo.coords_of(region)
    if latlon is None:
        return []

    out: list[str] = []
    target_date = (a.date or {}).get("date")
    try:
        from ..services import weather
        out += weather.checklist(latlon[0], latlon[1], target_date)
        if out and not geo.is_precise(region):
            out.append("※ 날씨는 시도 대표 좌표 기준입니다 — 실제 방문지와 다를 수 있습니다.")
    except Exception:
        pass
    try:
        from ..services import airquality
        out += airquality.checklist(region)
    except Exception:
        pass
    return out


GUARDIAN_CHANNEL = "앱·웹(보호자)"


def _build_card(phone, utterance, a, prof, hres, nres,
                channel: str = "전화", denied_owner: dict | None = None) -> card_mod.Card:
    # 보호자 웹으로 들어온 요청은 채널 자체가 '대리'라는 사실이다. 발화에서
    # 관계 호칭을 못 찾아도 마찬가지다 — 예전에는 "무릎이 아파서 정형외과
    # 가야 해요"처럼 호칭 없이 쓰면 본인 접수로 처리돼, 딸의 번호를 대상자
    # 번호로 보고 '신규 대상자(미등록 번호)'를 만들었다.
    requester = a.requester
    if channel == GUARDIAN_CHANNEL:
        requester = "대리"

    target = prof["name"] if prof else "신규 대상자(미등록 번호)"
    # 번호 주인이 아니라고 직접 밝혔다. 누구인지는 원문에만 있으므로 비워 두되,
    # 어느 번호로 왔는지는 남긴다 — 복지사가 되걸 곳이 그 번호뿐이다.
    if denied_owner is not None:
        target = f"미확인 ({denied_owner['name']} 님 번호)"
    # 표시 문자열과 별개로 상태를 필드로 남긴다. 예전에는 "대상자 후보 3명 —
    # 확인 필요" 같은 한글 문장이 유일한 단서라, 화면이 상태를 알려면 그 문장을
    # 파싱해야 했다.
    target_status = "확인됨" if prof else "확인 필요"
    target_evidence = ([f"발신번호가 등록된 케어 프로필과 일치 — {prof['name']}"] if prof
                       else ["발신번호가 등록된 대상자와 일치하지 않음"])
    if denied_owner is not None:
        target_evidence = [
            f"발신번호는 {denied_owner['name']} 님으로 등록돼 있으나, 통화에서 "
            "본인이 아니라고 응답(2번)",
            f"{denied_owner['name']} 님의 필요도·병원 이력은 적용하지 않음 — 원문 확인 필요",
        ]

    # 통화에서 성함·읍면동을 물었으면 그 답을 담는다.
    #
    # **번호로 대상자를 확정하지 못한 접수에서만** 채운다(미등록 번호, 본인이
    # 아니라고 응답). 박순자 님이 본인 번호로 걸어 "저는 박순자고요" 라고 말한
    # 것까지 따로 칸을 만들면 화면만 시끄러워진다.
    #
    # 물어놓고 답을 안 담으면 복지사가 매번 원문을 읽어야 한다 — 물어본 이유가
    # 화면에 없었다. 값은 언제나 '확인 필요' 이고 확정은 사람이 한다.
    spoken_name = spoken_region = None
    if prof is None:
        spoken_name = identity_mod.detect_name(utterance)
        spoken_region = identity_mod.detect_region(utterance)

    # 대리 접수 — 발신자와 대상자가 다르다. 대상자를 확정하지 않고 후보만 제시한다.
    candidates: list[dict] = []
    if requester == "대리":
        candidates = db.find_by_guardian_phone(phone)
        rel = f"{a.proxy_relation} " if a.proxy_relation else ""
        if len(candidates) == 1:
            target = f"{candidates[0]['name']} (보호자 대리 요청 — 확인 필요)"
            target_status = "추정"
            target_evidence = [f"보호자 연락처로 등록된 대상자 1명 — {candidates[0]['name']}"]
        elif len(candidates) > 1:
            target = f"대상자 후보 {len(candidates)}명 — 확인 필요"
            target_status = "확인 필요"
            target_evidence = [f"보호자 연락처로 등록된 대상자가 {len(candidates)}명 — 확정 불가"]
        else:
            target = f"미확인 대상자 ({rel}대리 요청)".replace("  ", " ")
            target_status = "확인 필요"
            target_evidence = [f"{rel or ''}대리 요청이지만 이 번호로 등록된 대상자가 없음".strip()]

    parts = [x for x in (a.dept,
                         a.date.get("label") if a.date else None,
                         f"{a.symptom} 관련" if a.symptom else None) if x]
    summary = f"{a.intent} 접수 — " + (", ".join(parts) if parts else "추가 정보 확인 필요")

    flags, questions = [], []
    if requester == "대리":
        flags.append("대리 요청: 대상자 확인 필요")
        rel = a.proxy_relation or "어르신"
        if candidates:
            names = " / ".join(c["name"] for c in candidates[:3])
            questions.append(f"{rel}이신 {names} 님 맞으실까요? 성함과 생년을 확인 부탁드립니다.")
        else:
            questions.append(f"{rel} 성함과 거주 읍면동을 알려주시면 대상자를 확인하겠습니다.")

    if hres.status in ("추정", "확인 필요"):
        flags.append("확인 필요: 병원명")
        hosp = hres.hospital or (hres.candidates[0]["hospital"] if hres.candidates else None)
        questions.append(f"어르신, 지난번 가셨던 {hosp} 맞으실까요?" if hosp
                         else "어르신, 어느 병원으로 모실지 확인 부탁드립니다.")
    if not (a.date and a.date.get("confident")):
        flags.append("확인 필요: 날짜")
        questions.append(_ambiguity_question(a.date or {}, "날짜"))

    # 시각은 없어도 접수를 막지 않는다. 다만 오전·오후를 알 수 없는 "3시"는
    # 우리가 골라주지 않고 되묻는다 — 잘못 고르면 반나절을 헛걸음한다.
    if a.time and not a.time.get("confident"):
        flags.append("확인 필요: 방문 시각")
        questions.append(_ambiguity_question(a.time, "시각"))
    elif not a.time:
        questions.append("방문 시각도 알려주시면 차량 배차에 반영하겠습니다.")

    mnotes = []
    if prof:
        if prof.get("notes"):
            mnotes.append(prof["notes"])
        if prof.get("preferred_time"):
            mnotes.append(f"{prof['preferred_time']} 방문 선호")

    dept = hres.dept or a.dept
    return card_mod.Card(
        target=target, phone_masked=card_mod.mask_phone(phone),
        raw_utterance=utterance, summary=summary, intent=a.intent,
        hospital=hres.hospital, hospital_status=hres.status,
        dept=dept,
        date_label=a.date.get("label") if a.date else None,
        date_value=a.date.get("date") if a.date else None,
        time_label=a.time.get("label") if a.time else None,
        time_value=a.time.get("time") if a.time else None,
        reasons=hres.reasons, confirm_questions=questions,
        need_level=nres.level, need_reasons=nres.reasons,
        need_basis=nres.basis, need_official=nres.official,
        guardian_contact=nres.guardian_contact,
        manager_notes=mnotes, flags=flags,
        requester=requester, proxy_relation=a.proxy_relation,
        target_candidates=candidates,
        spoken_name=spoken_name, spoken_region=spoken_region,
        field_status=_field_status(a, hres, target_status, dept),
        field_evidence=_field_evidence(a, hres, target_evidence, dept))


def _ambiguity_question(slot: dict, kind: str) -> str:
    """왜 확정하지 못했는지에 맞는 질문을 만든다.

    "10시나 11시"에 대고 "오전인가요 오후인가요"를 물으면 어긋난다.
    어르신이 실제로 말한 표현을 그대로 인용해서 되묻는다.
    """
    label, why = slot.get("label") or "", slot.get("ambiguous")
    if why == dateparse.AMBIGUOUS_MULTIPLE:
        return f"말씀하신 {label} 중에 어느 쪽으로 잡을까요?"
    if why == dateparse.AMBIGUOUS_NEGATED:
        return f"{josa(label, '은')} 아니라고 하셨는데, 그러면 언제로 잡을까요?"
    if kind == "시각":
        return f"말씀하신 {label}, 오전인가요 오후인가요?"
    return "방문 날짜를 한 번 더 확인 부탁드립니다."


def _field_status(a, hres, target_status: str, dept) -> dict[str, str]:
    return {
        "target": target_status,
        "hospital": hres.status,
        "dept": ("확인됨" if a.dept else "추정" if dept else "확인 필요"),
        "date": ("확인됨" if (a.date and a.date.get("confident")) else "확인 필요"),
        "time": ("확인됨" if (a.time and a.time.get("confident")) else "확인 필요"),
    }


def _field_evidence(a, hres, target_evidence: list[str], dept) -> dict[str, list[str]]:
    """항목마다 '왜 이 값인지'를 문장으로 남긴다. 확률은 쓰지 않는다."""
    if a.dept:
        dept_ev = [f"원문에서 '{a.dept}'를 직접 언급"]
        if a.symptom:
            dept_ev.append(f"증상 표현 '{a.symptom}' 확인")
    elif dept:
        dept_ev = [f"과거 이력의 진료과({dept})를 따름 — 확인 필요"]
    else:
        dept_ev = ["원문과 과거 이력만으로 진료과를 확인할 수 없음"]

    def when(slot: dict | None, kind: str) -> list[str]:
        if not slot:
            return [f"원문에서 방문 {josa(kind, '을')} 확인할 수 없음"]
        ev = [f"어르신이 '{slot['label']}'{particle(slot['label'], '이라고')} 직접 말함"]
        if slot.get("corrected"):
            ev.append("앞선 표현을 정정했으므로 마지막에 말한 것을 최종 의도로 봄")
        if not slot.get("confident"):
            ev.append("오전·오후를 말하지 않아 확정할 수 없음")
        return ev

    return {
        "target": target_evidence,
        "hospital": hres.reasons,
        "dept": dept_ev,
        "date": when(a.date, "날짜"),
        "time": when(a.time, "시각"),
    }
