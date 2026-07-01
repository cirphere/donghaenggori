"""사회복지사용 접수카드 생성 (계획서 기능 4).

접수카드 = 대상자·원문 발화·요약·병원 후보·상태·근거·확인 질문·동행 지원 수준·보호자 연락 여부
·매니저 전달사항. AI는 후보·근거까지만, 확정은 사람.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) >= 7:
        return f"{digits[:3]}-****-{digits[-4:]}"
    return "***"


@dataclass
class Card:
    target: str                      # 대상자 표시(이름/신규)
    phone_masked: str
    raw_utterance: str
    summary: str                     # AI 요약(한 줄)
    intent: str
    hospital: str | None
    hospital_status: str             # 확인됨/추정/확인 필요
    dept: str | None
    date_label: str | None
    date_value: str | None
    reasons: list[str]               # 근거(설명가능성)
    confirm_questions: list[str]     # 사회복지사 콜백용 확인 질문
    need_level: str                  # 동행 지원 수준 후보
    need_reasons: list[str]
    guardian_contact: bool
    manager_notes: list[str]
    flags: list[str] = field(default_factory=list)   # ⚠ 필수 확인 배지 등

    def to_text(self) -> str:
        L = []
        L.append("┌─────────── 사회복지사용 접수카드 ───────────")
        if self.flags:
            L.append("│ " + "  ".join(self.flags))
        L.append(f"│ 대상자   : {self.target}  ({self.phone_masked})")
        L.append(f"│ 원문 발화: \"{self.raw_utterance}\"")
        L.append(f"│ AI 요약  : {self.summary}")
        L.append(f"│ 요청 유형: {self.intent}")
        date_str = f"{self.date_label} ({self.date_value})" if self.date_value else "미확정"
        L.append(f"│ 방문 예정: {date_str}")
        hosp = self.hospital or "—"
        L.append(f"│ 병원 후보: {hosp}  [{self.hospital_status}]   진료과: {self.dept or '—'}")
        if self.reasons:
            L.append(f"│ 근거     : {' / '.join(self.reasons)}")
        if self.confirm_questions:
            L.append("│ 확인 질문(콜백):")
            for q in self.confirm_questions:
                L.append(f"│    · {q}")
        L.append(f"│ 동행 지원 수준(후보): {self.need_level}")
        if self.need_reasons:
            L.append(f"│    근거: {', '.join(self.need_reasons)}")
        L.append(f"│ 보호자 연락 필요: {'예' if self.guardian_contact else '아니오'}")
        if self.manager_notes:
            L.append(f"│ 매니저 전달: {' / '.join(self.manager_notes)}")
        L.append("│ ※ 병원명·일정·등급은 사회복지사가 최종 확인·확정합니다.")
        L.append("└────────────────────────────────────────────")
        return "\n".join(L)
