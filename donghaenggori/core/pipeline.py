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
from . import dateparse, db
from . import hospital as hospital_mod
from . import identity as identity_mod
from . import needlevel as need_mod
from . import nlu as nlu_mod
from . import requesttype as rt_mod
from .korean import josa, particle

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
    # 요청 유형(⑤-1). 의도와 **다른 축**이다 — 의도가 '병원동행'인 접수를 다시
    # 다섯으로 가른다. 긴급·약국·보호자연락에서는 None 이다.
    request: rt_mod.RequestType | None = None

    def to_dict(self) -> dict:
        return {
            "urgent": self.urgent,
            "channel": self.channel,
            "intent": self.analysis.intent,
            # 유형을 평면 키로도 낸다 — 목록·Inbox 가 카드를 열지 않고 배지를 그린다.
            "request_type": self.request.type if self.request else None,
            "request": self.request.to_dict() if self.request else None,
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
        identity_denied: bool = False,
        identity_utterance: str | None = None) -> Result:
    """identity_denied — 발신자가 **번호 주인이 아니라고 직접 밝힌** 경우.

    전화에서 "박순자 님 맞으신가요"에 2번을 누른 상황이다. 그때도 번호 주인의
    프로필로 카드를 채우면, 필요도(장기요양등급)와 병원 추천이 **다른 사람 것**
    으로 붙는다. 카드에는 '확인 필요' 가 뜨지만 내용 자체가 남의 정보라, 복지사가
    그 표시를 놓치면 엉뚱한 기준으로 동행을 준비하게 된다.

    아니라고 밝혔으면 그 말을 따른다. 프로필을 카드에 쓰지 않고, 발화에서 얻은
    것만 남긴다. 대상자 확정은 원문을 듣고 복지사가 한다.

    identity_utterance — 성함·읍면동을 **따로 물어 받은 답**. 전화에서는 문의
    내용과 다른 녹음으로 들어온다. 여기서 이름·주소를 뽑고, 접수 원문(utterance)
    에는 넣지 않는다 — 원문에 신상 이야기가 섞이면 복지사가 문의 내용을 찾아
    읽어야 한다. 없으면 문의 발화에서 뽑는 예전 방식으로 떨어진다(웹 등 한 번에
    받는 경로).
    """
    owner = db.get_profile(phone)                                  # ③④
    prof = None if identity_denied else owner
    c = _classify(utterance, use_llm, classifier)                  # ⑤
    a, source, conf, confident = c.analysis, c.source, c.confidence, c.urgent_confident

    # 대리 요청이면 발신자의 프로필을 쓰지 않는다 — 긴급이든 아니든 같다.
    # "우리 어매가 쓰러졌어" 를 발신자 이름으로 기록하면, 응급 기록을 보고
    # 움직이는 사람이 엉뚱한 어르신을 찾아간다.
    if a.requester == "대리":
        prof = None

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

    # ⑤-1 요청 유형. **긴급 판정 뒤에 온다** — 안전 동작을 새 유형이 가리면 안 된다.
    #
    # 의도가 '병원동행'인 접수만 다시 가른다. 약국·보호자연락은 이미 자기 흐름이
    # 있고(card.INTENT_FIELDS), 거기에 '신규 병원 탐색' 같은 유형을 얹으면 의미
    # 없는 조합이 생긴다.
    req = rt_mod.classify(utterance, a) if a.intent == "병원동행" else None
    new_type = req is not None and req.staff_handled

    if new_type:
        # **병원 후보를 만들지 않는다.** 어디로 갈지 몰라서 전화한 사람에게
        # 과거 단골을 '추정'으로 내미는 것이 이 유형에서 제일 흔한 사고다.
        hres = _no_hospital_guess(a, req)
    else:
        hres = hospital_mod.suggest(prof, a.dept, spoken=a.hospital, channel=channel)   # ⑥
    nres = need_mod.assess(prof)

    facilities: list[dict] = []
    if with_rag:                                                   # ⑦
        try:
            from ..services import rag
            facilities = rag.enrich(prof, a)
        except Exception:
            facilities = []

    # 검증된 목록에서만 후보를 찾는다. 조회가 안 되면 후보를 만들지 않고 사유를
    # 문장으로 받아 병원 칸 근거에 싣는다 — '모른다'가 화면에 남아야 한다.
    lookup_candidates: list[dict] = []
    lookup_note = ""
    if req is not None and req.type in rt_mod.LOOKUP_TYPES:
        lookup_candidates, lookup_note = _lookup_hospitals(prof, req)

    c = _build_card(phone, utterance, a, prof, hres, nres, channel,
                    denied_owner=owner if identity_denied else None,
                    identity_utterance=identity_utterance,
                    req=req, lookup_note=lookup_note)  # ⑧
    c.lookup_candidates = lookup_candidates
    c.outing_checklist = _outing_checklist(prof, a, c.spoken_region)
    if not new_type and hres.status == "확인 필요" and not (prof or {}).get("history"):
        c.reference_candidates = _reference_candidates(prof, a)
    return Result(urgent=False, card=c, analysis=a, profile=prof, channel=channel,
                  intent_source=source, intent_confidence=conf, facilities=facilities,
                  request=req)


def _no_hospital_guess(a, req) -> hospital_mod.HospitalResult:
    """새 유형에서 쓰는 병원 결과 — **후보 없음**을 근거와 함께 명시한다.

    이력이 있는 어르신이라도 여기서는 단골을 꺼내지 않는다. "새로 생긴 병원",
    "어떤 병원이 있는지 모르겠어"는 지난번 그 병원이 아니라는 말에 가깝다.
    진료과도 **직접 말했을 때만** 남긴다 — 증상에서 우리가 추정한 값으로 병원을
    고르기 시작하면, 우리 추정이 조회 조건이 되어 사실처럼 굳는다.
    """
    reasons = [f"'{req.type}' 요청 — 과거 이력의 단골을 이번 방문지 후보로 쓰지 않음"]
    if req.evidence:
        reasons.append("판단 근거: " + " / ".join(req.evidence))
    reasons.append("병원은 사회복지사가 어르신과 통화해 확인 — AI가 후보를 지어내지 않음")
    return hospital_mod.HospitalResult(
        status="확인 필요",
        hospital=None,
        dept=a.dept if getattr(a, "dept_source", None) == "spoken" else None,
        reasons=reasons,
        need_confirm=True)


def _lookup_hospitals(prof: dict | None, req) -> tuple[list[dict], str]:
    """검증된 병원 목록(심평원)에서만 후보를 찾는다.

    좌표는 케어 프로필의 지역에서 얻는다. 대상자가 확정되지 않은 접수(미등록
    번호·대리)는 어디서 찾을지 모르므로 **조회하지 않고 그 사실을 남긴다.**
    "우리 집 주변"이라는 말은 들었지만 그 집이 어디인지는 모르기 때문이다.
    """
    label = req.conditions.get("위치조건")
    try:
        from ..services import hospital_lookup
    except Exception as e:                       # 모듈이 없어도 접수는 계속된다
        return [], (f"병원 목록 조회를 하지 못했습니다({type(e).__name__}) — "
                    "사회복지사가 직접 확인해 주세요")

    latlon = None
    if prof and prof.get("region"):
        from . import geo
        latlon = geo.coords_of(_where(prof))
    try:
        res = hospital_lookup.lookup(
            dept=req.conditions.get("원하는진료과"),
            lat=latlon[0] if latlon else None,
            lon=latlon[1] if latlon else None,
            radius_m=REFERENCE_RADIUS_M, rows=REFERENCE_ROWS,
            location_label=label)
    except Exception as e:
        return [], (f"병원 목록 조회에 실패했습니다({type(e).__name__}) — "
                    "사회복지사가 직접 확인해 주세요")
    return res.candidates, res.note


def _where(prof: dict | None) -> str:
    """좌표를 뽑을 주소 문자열.

    **region 만 보면 안 된다.** 프로필에 상세주소(address)만 채워 넣은
    경우가 있는데, 그러면 날씨도 병원 후보도 조용히 아무것도 안 나온다 —
    주소를 적어 넣고도 왜 안 되는지 알 방법이 없다.

    둘을 이어 붙인다. geo 는 어절로 시군구를 찾으므로 앞뒤에 무엇이 붙어도
    되고(core/geo.py), 둘 중 하나만 있어도 그것으로 찾는다.
    """
    if not prof:
        return ""
    return " ".join(x for x in (prof.get("region"), prof.get("address")) if x).strip()


def _reference_candidates(prof: dict | None, a) -> list[dict]:
    """화면 04 4-A — 이력이 없을 때 거리 기준 '참고 후보'.

    확정 후보가 아니다. 근거가 거리뿐임을 각 항목에 명시하고, 사회복지사가
    확인전화로 확정하도록 남겨둔다. 외부 API가 없으면 빈 목록으로 폴백한다.
    """
    if not prof:
        return []
    from . import geo
    latlon = geo.coords_of(_where(prof))
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
    precise = geo.is_precise(_where(prof))
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


def _outing_checklist(prof: dict | None, a, spoken_region: str | None = None) -> list[str]:
    """외출 전 체크리스트 — 기상·대기 참고 정보.

    외부 API가 느리거나 미연동이면 조용히 건너뛴다. 접수 흐름을 막지 않는다.
    방문 가부는 판단하지 않고 참고 문구만 만든다.

    좌표는 병원 후보와 같은 geo.coords_of 를 쓴다. 예전에는 여기만 시도 대표
    좌표를 따로 들고 있었는데, 전남은 그 한 점이 목포 근처라 곡성 91km ·
    고흥 78km 떨어진 날씨를 보여줬다. 산간과 해안의 예보가 뒤바뀌는 거리다.

    **어디 기준인가.** 등록된 거주지를 먼저 쓰고, 없으면 통화에서 들은
    읍면동을 쓴다. 모든 어르신이 주소가 등록돼 있지는 않다 — 미등록 번호는
    프로필 자체가 없고, 등록돼 있어도 region 이 빈 경우가 있다. 그때 예전에는
    체크리스트가 통째로 사라졌다. 정작 그런 통화에서는 성함·읍면동을 따로
    물어 받아 두고도 쓰지 않고 있었다.

    **가는 병원이 아니라 출발지 기준이다.** 카드를 만드는 시점에 병원은
    아직 '추정'이거나 비어 있다. 확정되지 않은 병원 좌표로 날씨를 뽑으면
    틀린 곳의 날씨를 확신 있게 보여주게 된다. 우산·방한 준비는 집에서
    나설 때 정하는 것이기도 하다.

    어느 지역을 봤는지 마지막 줄에 **항상** 남긴다. 예전에는 시도 대표
    좌표일 때만 붙여서, 정밀할수록 기준이 화면에서 사라졌다 — 읽는 사람은
    그게 집 기준인지 병원 기준인지 알 수 없었다.
    """
    from . import geo
    region = _where(prof) or (spoken_region or "")
    region = region.strip()
    if not region:
        return []
    latlon = geo.coords_of(region)
    if latlon is None:
        return []

    out: list[str] = []
    target_date = (a.date or {}).get("date")
    try:
        from ..services import weather
        out += weather.checklist(latlon[0], latlon[1], target_date)
    except Exception:
        pass
    try:
        from ..services import airquality
        out += airquality.checklist(region)
    except Exception:
        pass

    if not out:
        return []

    기준 = "등록된 거주지" if _where(prof) else "통화에서 들은 주소"
    꼬리 = "" if geo.is_precise(region) else " · 시도 대표 좌표라 실제 방문지와 다를 수 있습니다"
    out.append(f"※ {기준}({region}) 기준입니다{꼬리}.")
    return out


# 확정하지 못한 사유 → 복지사가 읽을 문장. 무엇을 되물어야 하는지가
# 문장에 들어 있어야 한다.
_WHY_UNSURE = {
    dateparse.AMBIGUOUS_MERIDIEM: "오전·오후를 말하지 않아 확정할 수 없음",
    dateparse.AMBIGUOUS_MULTIPLE: "여러 날짜·시각을 말해 어느 쪽인지 확인 필요",
    dateparse.AMBIGUOUS_NEGATED: "말한 뒤 아니라고 해 확정할 수 없음",
}

GUARDIAN_CHANNEL = "앱·웹(보호자)"


def _named(prof: dict | None) -> bool:
    """쓸 수 있는 이름이 있는 프로필인가.

    **존재 여부만 보면 안 된다.** 주소·연락처만 채우고 이름은 비워 둔
    프로필이 실제로 만들어진다(기관이 명단부터 올리는 경우). 통화는 그때
    성함을 묻는데(web/voice._has_real_name), 파이프라인이 프로필이 있다는
    이유로 그 답을 버리면 **물어놓고 안 쓰는** 셈이 된다. 대상자는 빈 이름
    그대로 남고, 확정하면 이름 없는 프로필이 그대로 등록된다.

    판정 기준은 통화·등록과 같게 맞춘다.
    """
    name = ((prof or {}).get("name") or "").strip()
    if not name:
        return False
    return not any(k in name for k in ("미등록", "미확인", "신규 대상자", "후보"))


def _build_card(phone, utterance, a, prof, hres, nres,
                channel: str = "전화", denied_owner: dict | None = None,
                identity_utterance: str | None = None,
                req=None, lookup_note: str = "") -> card_mod.Card:
    # 보호자 웹으로 들어온 요청은 채널 자체가 '대리'라는 사실이다. 발화에서
    # 관계 호칭을 못 찾아도 마찬가지다 — 예전에는 "무릎이 아파서 정형외과
    # 가야 해요"처럼 호칭 없이 쓰면 본인 접수로 처리돼, 딸의 번호를 대상자
    # 번호로 보고 '신규 대상자(미등록 번호)'를 만들었다.
    requester = a.requester
    if channel == GUARDIAN_CHANNEL:
        requester = "대리"

    target = prof["name"] if _named(prof) else "신규 대상자(미등록 번호)"
    # 번호 주인이 아니라고 직접 밝혔다. 누구인지는 원문에만 있으므로 비워 두되,
    # 어느 번호로 왔는지는 남긴다 — 복지사가 되걸 곳이 그 번호뿐이다.
    if denied_owner is not None:
        target = f"미확인 ({denied_owner['name']} 님 번호)"
    # 표시 문자열과 별개로 상태를 필드로 남긴다. 예전에는 "대상자 후보 3명 —
    # 확인 필요" 같은 한글 문장이 유일한 단서라, 화면이 상태를 알려면 그 문장을
    # 파싱해야 했다.
    # 이름을 모르는 프로필은 '확인됨' 이 아니다. 발신번호가 맞아도 누구인지
    # 모르는 상태이고, 그대로 확정되면 이름 없는 프로필이 그대로 등록된다.
    target_status = "확인됨" if _named(prof) else "확인 필요"
    # 이름을 모르는 프로필이면 "일치 — " 뒤가 비어 문장이 끊긴다. 그때는
    # 무엇을 아는지(번호·주소)와 모르는지(성함)를 나눠 적는다.
    if _named(prof):
        target_evidence = [f"발신번호가 등록된 케어 프로필과 일치 — {prof['name']}"]
    elif prof:
        target_evidence = ["발신번호는 등록돼 있으나 케어 프로필에 성함이 없음 — "
                           "통화에서 여쭤 확인 필요"]
    else:
        target_evidence = ["발신번호가 등록된 대상자와 일치하지 않음"]
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
    #
    # 전화는 성함을 **따로 물어 받는다**(identity_utterance). 짧은 전용 답변이라
    # 긴 문장에서 골라내는 것보다 훨씬 정확하다. 없으면 문의 발화에서 뽑는다 —
    # 웹처럼 한 번에 받는 경로가 그렇다.
    spoken_name = spoken_region = None
    if not _named(prof):
        if identity_utterance:
            # 전용 답변이다 — 문장 전체가 답이라 훨씬 느슨하게 봐도 된다.
            # "이영희요" 처럼 이름만 툭 말하는 형태가 오히려 흔하다.
            spoken_name, spoken_region = identity_mod.parse_identity_answer(
                identity_utterance)
        else:
            spoken_name = identity_mod.detect_name(utterance)
            spoken_region = identity_mod.detect_region(utterance)

    # 대리 접수 — 발신자와 대상자가 다르다. 대상자를 확정하지 않고 후보만 제시한다.
    candidates: list[dict] = []
    if requester == "대리":
        candidates = db.find_by_guardian_phone(phone)
        rel = f"{a.proxy_relation} " if a.proxy_relation else ""
        if len(candidates) == 1:
            # **추정이 아니라 확인 필요다.** 다른 항목의 추정은 "근거를 대고 고른
            # 값"이라 틀려도 헛걸음으로 끝나지만, 여기서 고르는 것은 **누구의
            # 접수인가**다. 근거라고는 "이 번호로 등록된 사람이 한 명"뿐인데,
            # 그 한 명은 보호자가 앞서 신청해 등록된 어르신일 뿐이다. 그래서
            # 같은 보호자가 다른 부모를 신청하면 두 번째 접수가 첫 어르신
            # 이름으로 조용히 확정된다 — 실제로 재현했다(딸이 어머니 신청·확정
            # 뒤 아버지를 신청하니 target 이 어머니 이름으로 붙고 게이트는 통과).
            #
            # 값 문자열에 '확인 필요'라고 적어 두는 것으로는 아무것도 막지 못한다.
            # gate.blockers 는 status 만 본다. 물어볼 질문은 아래에서 이미 만든다
            # ("○○이신 △△ 님 맞으실까요?").
            target = f"{candidates[0]['name']} (보호자 대리 요청)"
            target_status = "확인 필요"
            target_evidence = [f"보호자 연락처로 등록된 대상자 1명 — {candidates[0]['name']}"]
        elif len(candidates) > 1:
            target = f"대상자 후보 {len(candidates)}명 — 확인 필요"
            target_status = "확인 필요"
            target_evidence = [f"보호자 연락처로 등록된 대상자가 {len(candidates)}명 — 확정 불가"]
        else:
            target = f"미확인 대상자 ({rel}대리 요청)".replace("  ", " ")
            target_status = "확인 필요"
            target_evidence = [f"{rel or ''}대리 요청이지만 이 번호로 등록된 대상자가 없음".strip()]

    # 새 유형이면 카드에 세울 칸이 달라진다(card.REQUEST_TYPE_FIELDS). 화면·게이트가
    # 각자 판단하지 않도록 여기서 한 번만 물어보고, 질문·플래그도 그 목록을 따른다.
    rtype = req.type if req is not None else None
    new_type = req is not None and req.staff_handled
    shown = card_mod.fields_for(a.intent, rtype) or tuple(card_mod.FIELD_VALUE_ATTRS)

    if new_type:
        # 목록에서 한 줄로 읽히는 것이 이 요약의 목적이다. 무엇을 요청했는지와
        # **사람이 응대해야 한다**는 사실이 둘 다 보여야 한다.
        summary = f"신규 유형 요청 — {req.summary()} · 사회복지사 직접 응대 필요"
    else:
        parts = [x for x in (a.dept,
                             a.date.get("label") if a.date else None,
                             f"{a.symptom} 관련" if a.symptom else None) if x]
        summary = f"{a.intent} 접수 — " + (", ".join(parts) if parts else "추가 정보 확인 필요")

    # 진료과를 아는가 — **직접 말했을 때만** 안다고 본다. 증상 사전이나 임베딩이
    # 고른 값은 우리 추정이라, 되물어 확인할 대상이다.
    dept_known = getattr(a, "dept_source", None) == "spoken"

    flags, questions = [], []
    if new_type:
        # Inbox 배지가 읽는 문구. 접수 목록에서 이 한 줄로 새 유형이 갈린다.
        flags.append("새로운 유형의 요청입니다 — 사회복지사 직접 응대")
        # 게이트가 이 질문을 blockers 에 실어 화면에 띄운다(gate._QUESTION_HINTS).
        # '병원'이라는 말을 넣지 않는다 — 어디로 갈지 몰라 전화한 어르신에게
        # 물을 말이 아니고, 병원 칸의 질문으로 잘못 골라지기도 한다.
        questions.append("새로운 유형의 요청입니다 — 어떤 도움이 필요하신지 "
                         "사회복지사가 직접 확인해 주세요.")
    if requester == "대리":
        flags.append("대리 요청: 대상자 확인 필요")
        rel = a.proxy_relation or "어르신"
        if candidates:
            names = " / ".join(c["name"] for c in candidates[:3])
            questions.append(f"{rel}이신 {names} 님 맞으실까요? 성함과 생년을 확인 부탁드립니다.")
        else:
            questions.append(f"{rel} 성함과 거주 읍면동을 알려주시면 대상자를 확인하겠습니다.")

    # 카드에 세우지 않은 칸은 묻지 않는다. 새 유형에서 "지난번 가셨던 ○○병원
    # 맞으실까요?"를 띄우면, 어디로 갈지 몰라 전화한 어르신에게 우리가 고른 병원을
    # 되묻는 셈이 된다. 병원은 위 요청 질문 하나로 함께 확인한다.
    if "hospital" in shown and not new_type and hres.status in ("추정", "확인 필요"):
        flags.append("확인 필요: 병원명")
        hosp = hres.hospital or (hres.candidates[0]["hospital"] if hres.candidates else None)
        if hres.dept_mismatch:
            # **이력의 병원을 되묻지 않는다.** 어르신이 말한 진료과가 그 병원에
            # 있는지 우리는 모른다. 실통화에서 "지난번 가셨던 백병원 맞으실까요?"
            # 를 피부과 요청에 물었고, "거기 피부과 없어요" 라는 답을 받고도
            # 백병원으로 접수했다. 물을 것은 '그 병원이 맞냐' 가 아니라
            # '어느 병원이냐' 다.
            questions.append(f"말씀하신 {a.dept}는 어느 병원으로 모실까요?" if a.dept
                             else "어느 병원으로 모실지 알려주시겠어요?")
        else:
            questions.append(f"어르신, 지난번 가셨던 {hosp} 맞으실까요?" if hosp
                             else "어르신, 어느 병원으로 모실지 확인 부탁드립니다.")
    # 진료과는 확정을 막지 않지만(gate.BLOCKING) **통화에서는 되묻는다.**
    # 동행 정보에서 빠지면 복지사가 다시 전화해야 하는 항목이라, 어르신이 아직
    # 통화 중일 때 한 번 물어보는 편이 낫다. 모르실 수도 있고, 그때는 확인
    # 필요로 남을 뿐이다 — 우리가 채우지 않는다.
    if "dept" in shown and not dept_known:
        questions.append("어느 과로 가시는지 알고 계신가요?")
    if "date" in shown and not (a.date and a.date.get("confident")):
        flags.append("확인 필요: 날짜")
        questions.append(_ambiguity_question(a.date or {}, "날짜", channel))

    # 시각은 없어도 접수를 막지 않는다. 다만 오전·오후를 알 수 없는 "3시"는
    # 우리가 골라주지 않고 되묻는다 — 잘못 고르면 반나절을 헛걸음한다.
    if "time" in shown:
        if a.time and not a.time.get("confident"):
            flags.append("확인 필요: 방문 시각")
            questions.append(_ambiguity_question(a.time, "시각", channel))
        elif not a.time:
            questions.append("방문 시각도 알려주시면 차량 배차에 반영하겠습니다.")

    mnotes = []
    if prof:
        if prof.get("notes"):
            mnotes.append(prof["notes"])
        if prof.get("preferred_time"):
            mnotes.append(f"{prof['preferred_time']} 방문 선호")

    if new_type:
        # 증상에서 우리가 추정한 진료과는 카드에 싣지 않는다. "다리가 불편" 을
        # 정형외과로 옮기는 것은 우리 판단인데, 그 값이 카드에 앉으면 병원 조회
        # 조건이 되고 배차 기준이 되어 사실처럼 굳는다. 직접 말한 것만 남긴다.
        dept = a.dept if getattr(a, "dept_source", None) == "spoken" else None
    else:
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
        # 기관이 이미 아는 값 — 확신도를 붙이지 않고 그대로 싣는다.
        # 대상자가 확정되지 않았으면(미등록·대리) 프로필이 없거나 남의
        # 것일 수 있으므로 아무것도 싣지 않는다.
        # region 과 address 를 이어 붙인다. address 만 쓰면 '여서로' 처럼
        # 시군구가 빠져 어디인지 알 수 없다.
        pickup=_where(prof) or None,
        mobility=prof.get("mobility") if prof else None,
        guardian=prof.get("guardian") if prof else None,
        caregiver=prof.get("caregiver") if prof else None,
        manager_notes=mnotes, flags=flags,
        requester=requester, proxy_relation=a.proxy_relation,
        target_candidates=candidates,
        spoken_name=spoken_name, spoken_region=spoken_region,
        request_type=rtype,
        request_summary=req.summary() if new_type else None,
        request_evidence=list(req.evidence) if new_type else [],
        request_conditions=dict(req.conditions) if new_type else {},
        field_status=_field_status(a, hres, target_status, dept, req),
        field_evidence=_field_evidence(a, hres, target_evidence, dept, channel,
                                       req, lookup_note))


def _ambiguity_question(slot: dict, kind: str, channel: str = "전화") -> str:
    """왜 확정하지 못했는지에 맞는 질문을 만든다.

    "10시나 11시"에 대고 "오전인가요 오후인가요"를 물으면 어긋난다.
    어르신이 실제로 말한 표현을 그대로 인용해서 되묻는다.
    """
    label, why = slot.get("label") or "", slot.get("ambiguous")
    # 보호자가 폼에 적은 것을 "말씀하신" 이라고 되물으면 어긋난다.
    said = "적어 주신" if channel == GUARDIAN_CHANNEL else "말씀하신"
    if why == dateparse.AMBIGUOUS_MULTIPLE:
        return f"{said} {label} 중에 어느 쪽으로 잡을까요?"
    if why == dateparse.AMBIGUOUS_NEGATED:
        return f"{josa(label, '은')} 아니라고 하셨는데, 그러면 언제로 잡을까요?"
    if kind == "시각":
        return f"{said} {label}, 오전인가요 오후인가요?"
    return "방문 날짜를 한 번 더 확인 부탁드립니다."


def _field_status(a, hres, target_status: str, dept, req=None) -> dict[str, str]:
    if req is not None and req.staff_handled:
        # 새 유형은 우리가 채운 값이 없다. **'추정'조차 주지 않는다** — 추정은
        # "근거를 대고 고른 값"인데 여기서는 고른 것이 없다. 진료과만 어르신이
        # 직접 말했으면 확인됨이다.
        return {
            "target": target_status,
            "request": "확인 필요",
            "hospital": "확인 필요",
            "dept": "확인됨" if getattr(a, "dept_source", None) == "spoken" else "확인 필요",
            "date": ("확인됨" if (a.date and a.date.get("confident")) else "확인 필요"),
            "time": ("확인됨" if (a.time and a.time.get("confident")) else "확인 필요"),
        }
    return {
        "target": target_status,
        "hospital": hres.status,
        # 진료과를 어떻게 얻었는지로 가른다. 어르신이 "정형외과" 라고 직접
        # 말했으면 확인됨이지만, 증상 사전이나 임베딩으로 우리가 고른 것은
        # 추정이다 — "손발이 저려" 를 신경과로 잇는 것은 우리 판단이지
        # 어르신이 말한 것이 아니다. 예전에는 둘을 구분하지 않아, 유사도
        # 0.67 로 고른 값이 카드에 '확인됨' 으로 떴다.
        "dept": ("확인됨" if getattr(a, "dept_source", None) == "spoken"
                 else "추정" if (a.dept or dept) else "확인 필요"),
        "date": ("확인됨" if (a.date and a.date.get("confident")) else "확인 필요"),
        "time": ("확인됨" if (a.time and a.time.get("confident")) else "확인 필요"),
    }


def _field_evidence(a, hres, target_evidence: list[str], dept,
                    channel: str = "전화", req=None,
                    lookup_note: str = "") -> dict[str, list[str]]:
    """항목마다 '왜 이 값인지'를 문장으로 남긴다. 확률은 쓰지 않는다.

    새 유형에서는 **왜 값이 없는지**가 더 중요하다. 빈 칸만 있으면 복지사는
    "AI가 못 찾았나 안 찾았나"를 알 수 없고, 조회가 미연동이라 비어 있는 것을
    "이 지역에 병원이 없다"로 읽을 수도 있다.
    """
    if a.dept:
        # 병원 근거와 같은 이유로 경로를 가른다(hospital.suggest 주석 참고).
        dept_ev = [f"신청서에 '{a.dept}'를 직접 입력" if channel == GUARDIAN_CHANNEL
                   else f"원문에서 '{a.dept}'를 직접 언급"]
        if a.symptom:
            dept_ev.append(f"증상 표현 '{a.symptom}' 확인")
    elif dept:
        dept_ev = [f"과거 이력의 진료과({dept})를 따름 — 확인 필요"]
    else:
        dept_ev = ["원문과 과거 이력만으로 진료과를 확인할 수 없음"]

    def when(slot: dict | None, kind: str) -> list[str]:
        if not slot:
            return [f"{'신청서' if channel == GUARDIAN_CHANNEL else '원문'}에서"
                    f" 방문 {josa(kind, '을')} 확인할 수 없음"]
        if channel == GUARDIAN_CHANNEL:
            # 보호자는 달력에서 고른다. 그 값을 "직접 말함" 으로 적으면
            # 통화에서 들은 것이 된다.
            ev = [f"보호자가 신청서에 '{slot['label']}'"
                  f"{particle(slot['label'], '을')} 선택"]
        else:
            ev = [f"어르신이 '{slot['label']}'{particle(slot['label'], '이라고')} 직접 말함"]
        if slot.get("corrected"):
            ev.append("앞선 표현을 정정했으므로 마지막에 말한 것을 최종 의도로 봄")
        if not slot.get("confident"):
            # **왜 확정 못 했는지를 그 항목에 맞게 적는다.**
            #
            # 이 함수는 날짜와 시각이 같이 쓴다. 예전에는 무엇이든
            # "오전·오후를 말하지 않아" 라고 적어서, 날짜 칸에 시각 이야기가
            # 붙었다 — "9월 5일 / 금요일" 아래에 오전·오후 문구가 떴다.
            # 복지사가 읽고 무엇을 물어야 할지 알 수 없는 근거는 없느니만
            # 못하다. 파서가 이미 사유를 알고 있으니 그것을 쓴다.
            ev.append(_WHY_UNSURE.get(slot.get("ambiguous"), "")
                      or f"말씀하신 {kind}{josa(kind, '을')} 하나로 확정할 수 없음")
        return ev

    out = {
        "target": target_evidence,
        "hospital": hres.reasons,
        "dept": dept_ev,
        "date": when(a.date, "날짜"),
        "time": when(a.time, "시각"),
    }
    if req is not None and req.staff_handled:
        out["request"] = list(req.evidence) + [
            f"기존 접수 흐름이 다루지 않는 요청 유형 — {rt_mod.STAFF_STATUS}",
            "AI는 이 요청의 병원·진료과·인력 정보를 만들지 않습니다",
        ]
        if lookup_note:
            out["hospital"] = list(hres.reasons) + [lookup_note]
        if getattr(a, "dept_source", None) != "spoken":
            # 사전·임베딩으로 고른 진료과가 있어도 카드에는 안 싣는다. 다만 무엇을
            # 보고 그렇게 생각했는지는 남긴다 — 복지사가 통화할 때 단서가 된다.
            out["dept"] = ([f"증상 표현 '{a.symptom}' 확인 — 진료과는 어르신이 "
                            "말하지 않아 카드에 싣지 않음"] if a.symptom
                           else ["어르신이 진료과를 말하지 않음 — 사회복지사가 확인"])
    return out
