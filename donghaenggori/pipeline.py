"""전체 처리 흐름 오케스트레이션 (계획서 4장 작동 흐름 ①~⑧, ⑨부터 사람).

run(phone, utterance) → 접수카드(Card) + 중간 산출물(dict).
긴급 신호 감지 시 접수를 만들지 않고 '사람 연결 필요'로 전환한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import card as card_mod
from . import hospital as hospital_mod
from . import needlevel as need_mod
from . import nlu as nlu_mod
from . import profile as profile_mod


@dataclass
class Result:
    urgent: bool
    card: card_mod.Card | None
    analysis: nlu_mod.Analysis
    profile: dict | None
    urgent_message: str | None = None


def run(phone: str, utterance: str, use_llm: bool | None = None) -> Result:
    # ③ 케어 프로필 조회
    prof = profile_mod.lookup(phone)
    # ⑤ 발화 의도 분류 (하이브리드 NLU)
    a = nlu_mod.analyze(utterance, use_llm=use_llm)

    # 긴급: 접수 중단 → 사람/119 연결 (AI는 응급 여부 판단 안 함)
    if a.urgent:
        msg = ("⚠ 긴급 신호 감지 — 즉시 담당자/119 연결 필요. "
               "AI는 응급 여부를 판단하지 않고 사람에게 넘깁니다.")
        return Result(urgent=True, card=None, analysis=a, profile=prof, urgent_message=msg)

    # 약국·보호자연락 등 비(非)병원동행은 간단 처리(데모 범위에선 병원동행 중심)
    # ⑥ 병원 후보 생성(과거 이력 기반, 3단계 상태)
    hres = hospital_mod.suggest(prof, a.dept)
    # 동행 지원 수준 후보
    nres = need_mod.assess(prof)

    c = _build_card(phone, utterance, a, prof, hres, nres)
    return Result(urgent=False, card=c, analysis=a, profile=prof)


def _build_card(phone, utterance, a, prof, hres, nres) -> card_mod.Card:
    target = prof["name"] if prof else "신규 대상자(미등록 번호)"

    # 요약
    parts = []
    if a.dept:
        parts.append(a.dept)
    if a.date and a.date.get("label"):
        parts.append(a.date["label"])
    if a.symptom:
        parts.append(f"{a.symptom} 관련")
    summary = f"{a.intent} 접수 — " + (", ".join(parts) if parts else "추가 정보 확인 필요")

    # 확인 질문 + 필수 확인 배지
    flags, questions = [], []
    if hres.status in ("추정", "확인 필요"):
        flags.append("⚠필수확인: 병원명")
        hosp = hres.hospital or (hres.candidates[0]["hospital"] if hres.candidates else None)
        if hosp:
            questions.append(f"어르신, 지난번 가셨던 {hosp} 맞으실까요?")
        else:
            questions.append("어르신, 어느 병원으로 모실지 확인 부탁드립니다.")
    if not (a.date and a.date.get("confident")):
        flags.append("⚠필수확인: 날짜")
        questions.append("방문 날짜를 한 번 더 확인 부탁드립니다.")

    # 매니저 전달
    mnotes = []
    if prof:
        if prof.get("notes"):
            mnotes.append(prof["notes"])
        if prof.get("preferred_time"):
            mnotes.append(f"{prof['preferred_time']} 방문 선호")
        if "섬" in (prof.get("region") or ""):
            mnotes.append("섬 지역 — 배편 시간 확인")

    return card_mod.Card(
        target=target,
        phone_masked=card_mod.mask_phone(phone),
        raw_utterance=utterance,
        summary=summary,
        intent=a.intent,
        hospital=hres.hospital,
        hospital_status=hres.status,
        dept=hres.dept or a.dept,
        date_label=a.date.get("label") if a.date else None,
        date_value=a.date.get("date") if a.date else None,
        reasons=hres.reasons,
        confirm_questions=questions,
        need_level=nres.level,
        need_reasons=nres.reasons,
        guardian_contact=nres.guardian_contact,
        manager_notes=mnotes,
        flags=flags,
    )
