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
from . import db
from . import hospital as hospital_mod
from . import needlevel as need_mod
from . import nlu as nlu_mod

CHANNELS = ("전화", "앱·웹(보호자)", "직접(기관)")

# 긴급 임계값(0.06)은 재현율 우선으로 잡혀 있어 낮다. 그 아래로는 못 내리지만,
# 낮은 점수까지 "긴급"이라고 단정하면 문제가 생긴다 — 학습 분포 밖의 발화
# (인사·잡담·STT 오인식)가 0.06~0.13 구간에 흩어져 전부 긴급으로 뜬다.
#   실측: "고맙습니다 수고하세요" 0.112 · "여보세요?" 0.068 · 실제 긴급 0.994
#   홀드아웃 실제 긴급의 95%가 0.97 이상이다.
# 그래서 임계값은 유지하고(재현율 0.993 그대로), 이 값 미만은 '판단 보류'로
# 구분한다. 사람에게 넘기는 안전 동작은 같고, 단정만 하지 않는다.
URGENT_CONFIDENT = 0.5

# 약국·보호자연락은 학습 모델에 실데이터가 없다. C-DS01 에 대응 카테고리가
# 없어서 train_intent 가 규칙 사전으로 만든 합성 템플릿만 넣고 학습했다.
# 그래서 템플릿 밖 표현이 오면 모델이 무너진다 — 사전이 잡는 8문장으로 재보니
# 규칙 8/8, 모델 2/8 이었고, 틀릴 때도 확신은 높았다(0.993 · 0.939 · 0.906).
# 확신도 임계값으로 거를 수 없다는 뜻이다. 이 두 의도만큼은 사전이 직접 잡은
# 결과를 유지한다. 병원동행·기타는 실데이터로 학습했으므로 모델을 따른다.
RULE_OWNED_INTENTS = ("약국", "보호자연락")


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


def _classify(utterance: str, use_llm: bool | None):
    """학습 모델 우선 → 규칙 NLU 폴백. 슬롯(진료과·증상·날짜)은 규칙이 담당한다.

    반환의 마지막 값은 '긴급을 단정할 수 있는가'다. 규칙 사전이 직접 잡았거나
    모델 점수가 충분히 높을 때만 True — 자세한 이유는 URGENT_CONFIDENT 참조.
    """
    a = nlu_mod.analyze(utterance, use_llm=use_llm)
    source, conf = a.source, None
    rule_urgent = a.urgent          # 사전이 직접 잡은 것은 근거가 명확하다
    rule_intent = a.intent

    try:
        from ..services import intent_model
        pred = intent_model.predict(utterance)
    except Exception:
        pred = None

    urgent_score = None
    if pred is not None:
        # 긴급은 어느 쪽이든 하나라도 걸리면 긴급 (재현율 우선)
        a.urgent = a.urgent or pred.urgent
        source, conf = "학습모델", pred.confidence
        urgent_score = pred.urgent_score
        if a.urgent:
            a.intent = "긴급"
        elif rule_intent in RULE_OWNED_INTENTS and pred.intent != rule_intent:
            # 사전이 잡은 의도를 모델이 뒤집으려 한다 — RULE_OWNED_INTENTS 참조.
            # 무엇을 근거로 정했는지 화면에 그대로 드러낸다.
            a.intent = rule_intent
            source, conf = "규칙 사전(모델과 불일치)", None
        else:
            a.intent = pred.intent

    confident = rule_urgent or (urgent_score is not None and urgent_score >= URGENT_CONFIDENT)
    return a, source, conf, confident


def run(phone: str, utterance: str, channel: str = "전화",
        use_llm: bool | None = None, with_rag: bool = True) -> Result:
    prof = db.get_profile(phone)                                   # ③④
    a, source, conf, confident = _classify(utterance, use_llm)     # ⑤

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

    hres = hospital_mod.suggest(prof, a.dept)                      # ⑥
    nres = need_mod.assess(prof)

    facilities: list[dict] = []
    if with_rag:                                                   # ⑦
        try:
            from ..services import rag
            facilities = rag.enrich(prof, a)
        except Exception:
            facilities = []

    c = _build_card(phone, utterance, a, prof, hres, nres)         # ⑧
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
        res = hira.nearby(latlon[0], latlon[1], dept=a.dept, radius_m=6000, rows=3)
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


def _build_card(phone, utterance, a, prof, hres, nres) -> card_mod.Card:
    target = prof["name"] if prof else "신규 대상자(미등록 번호)"
    # 표시 문자열과 별개로 상태를 필드로 남긴다. 예전에는 "대상자 후보 3명 —
    # 확인 필요" 같은 한글 문장이 유일한 단서라, 화면이 상태를 알려면 그 문장을
    # 파싱해야 했다.
    target_status = "확인됨" if prof else "확인 필요"
    target_evidence = ([f"발신번호가 등록된 케어 프로필과 일치 — {prof['name']}"] if prof
                       else ["발신번호가 등록된 대상자와 일치하지 않음"])

    # 대리 접수 — 발신자와 대상자가 다르다. 대상자를 확정하지 않고 후보만 제시한다.
    candidates: list[dict] = []
    if a.requester == "대리":
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
    if a.requester == "대리":
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
        questions.append("방문 날짜를 한 번 더 확인 부탁드립니다.")

    # 시각은 없어도 접수를 막지 않는다. 다만 오전·오후를 알 수 없는 "3시"는
    # 우리가 골라주지 않고 되묻는다 — 잘못 고르면 반나절을 헛걸음한다.
    if a.time and not a.time.get("confident"):
        flags.append("확인 필요: 방문 시각")
        questions.append(f"말씀하신 {a.time['label']}이 오전인가요, 오후인가요?")
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
        guardian_contact=nres.guardian_contact,
        manager_notes=mnotes, flags=flags,
        requester=a.requester, proxy_relation=a.proxy_relation,
        target_candidates=candidates,
        field_status=_field_status(a, hres, target_status, dept),
        field_evidence=_field_evidence(a, hres, target_evidence, dept))


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
            return [f"원문에서 방문 {kind}을 확인할 수 없음"]
        ev = [f"어르신이 '{slot['label']}'이라고 직접 말함"]
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
