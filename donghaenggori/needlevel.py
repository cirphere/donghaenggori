"""동행 지원 수준 후보 산출 — 룰엔진 (계획서 4장: 의료 판단 아닌 '운영 등급').

독거·거동불편·낙상위험·보호자부재·이동특성을 점수화해 후보를 제시한다.
최종 등급은 사회복지사가 확정한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 운영 등급 후보
LEVELS = ["단순 안내", "차량+동행", "휠체어·부축 동행"]


@dataclass
class NeedResult:
    level: str                         # 후보 등급
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    guardian_contact: bool = False     # 보호자 연락 필요 여부


def assess(profile: dict | None) -> NeedResult:
    if not profile:
        return NeedResult(
            level="확인 필요",
            reasons=["신규 대상자 — 이동 특성 파악 후 사회복지사가 등급 결정"],
            guardian_contact=False,
        )

    score = 0
    reasons: list[str] = []

    mobility = (profile.get("mobility") or "")
    if "휠체어" in mobility:
        score += 3
        reasons.append("휠체어 필요")
    elif "거동 불편" in mobility or "보행기" in mobility:
        score += 2
        reasons.append("거동 불편")
    elif "차량" in mobility:
        score += 1
        reasons.append("장거리 이동 시 차량 필요")

    if profile.get("fall_risk"):
        score += 1
        reasons.append("낙상 위험")
    if profile.get("lives_alone"):
        score += 1
        reasons.append("독거")

    guardian = profile.get("guardian")
    guardian_contact = guardian is None
    if guardian is None:
        score += 1
        reasons.append("보호자 없음 — 동행 매니저 필수")

    if score >= 4:
        level = "휠체어·부축 동행"
    elif score >= 2:
        level = "차량+동행"
    else:
        level = "단순 안내"

    return NeedResult(level=level, score=score, reasons=reasons, guardian_contact=guardian_contact)
