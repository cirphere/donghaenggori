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


@dataclass
class Result:
    urgent: bool
    card: card_mod.Card | None
    analysis: nlu_mod.Analysis
    profile: dict | None
    channel: str = "전화"
    urgent_message: str | None = None
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
            "facilities": self.facilities,
            "card": self.card.to_dict() if self.card else None,
        }


def _classify(utterance: str, use_llm: bool | None):
    """학습 모델 우선 → 규칙 NLU 폴백. 슬롯(진료과·증상·날짜)은 규칙이 담당한다."""
    a = nlu_mod.analyze(utterance, use_llm=use_llm)
    source, conf = a.source, None

    try:
        from ..services import intent_model
        pred = intent_model.predict(utterance)
    except Exception:
        pred = None

    if pred is not None:
        # 긴급은 어느 쪽이든 하나라도 걸리면 긴급 (재현율 우선)
        a.urgent = a.urgent or pred.urgent
        a.intent = "긴급" if a.urgent else pred.intent
        source, conf = "학습모델", pred.confidence
    return a, source, conf


def run(phone: str, utterance: str, channel: str = "전화",
        use_llm: bool | None = None, with_rag: bool = True) -> Result:
    prof = db.get_profile(phone)                                   # ③④
    a, source, conf = _classify(utterance, use_llm)                # ⑤

    if a.urgent:                                                   # 긴급 → 접수 중단
        return Result(
            urgent=True, card=None, analysis=a, profile=prof, channel=channel,
            urgent_message=("긴급 신호 감지 — 접수카드 생성을 중단하고 즉시 담당자·사람 상담으로 연결합니다. "
                            "동행고리 AI는 응급 여부를 판단하지 않습니다."),
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
    return Result(urgent=False, card=c, analysis=a, profile=prof, channel=channel,
                  intent_source=source, intent_confidence=conf, facilities=facilities)


# 시도별 대표 좌표 — 기상 격자 변환용. 읍면동 좌표가 없는 시연 데이터를 위한 근사값.
_REGION_LATLON = {
    "광주": (35.1601, 126.8514),
    "전남": (34.8161, 126.4630),
}


def _outing_checklist(prof: dict | None, a) -> list[str]:
    """외출 전 체크리스트 — 기상·대기 참고 정보.

    외부 API가 느리거나 미연동이면 조용히 건너뛴다. 접수 흐름을 막지 않는다.
    방문 가부는 판단하지 않고 참고 문구만 만든다.
    """
    if not prof:
        return []
    region = prof.get("region") or ""
    latlon = next((v for k, v in _REGION_LATLON.items() if region.startswith(k)), None)
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
    return out


def _build_card(phone, utterance, a, prof, hres, nres) -> card_mod.Card:
    target = prof["name"] if prof else "신규 대상자(미등록 번호)"

    # 대리 접수 — 발신자와 대상자가 다르다. 대상자를 확정하지 않고 후보만 제시한다.
    candidates: list[dict] = []
    if a.requester == "대리":
        candidates = db.find_by_guardian_phone(phone)
        rel = f"{a.proxy_relation} " if a.proxy_relation else ""
        if len(candidates) == 1:
            target = f"{candidates[0]['name']} (보호자 대리 요청 — 확인 필요)"
        elif len(candidates) > 1:
            target = f"대상자 후보 {len(candidates)}명 — 확인 필요"
        else:
            target = f"미확인 대상자 ({rel}대리 요청)".replace("  ", " ")

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

    mnotes = []
    if prof:
        if prof.get("notes"):
            mnotes.append(prof["notes"])
        if prof.get("preferred_time"):
            mnotes.append(f"{prof['preferred_time']} 방문 선호")

    return card_mod.Card(
        target=target, phone_masked=card_mod.mask_phone(phone),
        raw_utterance=utterance, summary=summary, intent=a.intent,
        hospital=hres.hospital, hospital_status=hres.status,
        dept=hres.dept or a.dept,
        date_label=a.date.get("label") if a.date else None,
        date_value=a.date.get("date") if a.date else None,
        reasons=hres.reasons, confirm_questions=questions,
        need_level=nres.level, need_reasons=nres.reasons,
        guardian_contact=nres.guardian_contact,
        manager_notes=mnotes, flags=flags,
        requester=a.requester, proxy_relation=a.proxy_relation,
        target_candidates=candidates)
