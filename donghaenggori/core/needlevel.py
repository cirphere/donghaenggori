"""동행 지원 수준 후보 산출 (계획서 4장: 의료 판단이 아닌 '운영 등급').

**근거의 출처를 먼저 밝힌다.** 예전에는 휠체어 3점·독거 1점처럼 우리가 지어낸
가중치로 합산했다. 잘 돌아가긴 했지만 "왜 휠체어가 3점입니까"에 답할 수 없었다.
지어낸 숫자로 사람의 지원 수준을 정하는 셈이라, 심사에서든 실제 운영에서든
방어할 수 없다.

그래서 순서를 바꿨다.

  1순위  장기요양등급 (노인장기요양보험법) — 국가가 이미 판정한 기능 상태
  2순위  노인맞춤돌봄서비스 대상자 군 — 지자체가 이미 분류한 돌봄 필요도
  3순위  관찰 특성 (거동·낙상·독거·보호자 부재) — **위 둘이 없을 때만**

1·2순위는 공식 판정을 그대로 옮기는 것이라 근거를 물으면 출처를 댈 수 있다.
3순위는 우리 휴리스틱이므로 결과에 그렇게 표시하고, 사회복지사가 확정 전에
공식 등급을 확인하도록 안내한다. AI가 등급을 '판정'하지 않는다는 원칙은 그대로다 —
어느 경로든 후보일 뿐이고 확정은 사람이 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 운영 등급 후보 — 우리 서비스의 배차·인력 배정 단위다. 복지 등급이 아니다.
LEVELS = ["단순 안내", "차량+동행", "휠체어·부축 동행"]

# 근거의 출처. 화면과 접수카드에 그대로 노출한다.
BASIS_LTCI = "장기요양등급"
BASIS_CARE_PROGRAM = "노인맞춤돌봄 대상자 군"
BASIS_OBSERVED = "관찰 특성(공식 등급 미확인)"
BASIS_NONE = "정보 없음"

# 장기요양등급 → 운영 등급.
# 1·2등급은 일상생활 전반에 상시 도움이 필요한 상태로 판정된 구간이라
# 부축·휠체어 동행을 기본으로 둔다. 3~5등급은 부분 도움이므로 차량+동행.
# 인지지원등급은 신체 기능보다 인지 저하가 쟁점이라 이동 자체보다 동반·안내가
# 핵심이다 — 등급만으로 신체 보조 수준을 올리지 않고 주의사항으로 남긴다.
_LTCI_LEVEL = {
    "1": "휠체어·부축 동행",
    "2": "휠체어·부축 동행",
    "3": "차량+동행",
    "4": "차량+동행",
    "5": "차량+동행",
    "인지지원": "차량+동행",
}

# 노인맞춤돌봄서비스 군 → 운영 등급.
# 중점돌봄군은 신체적 제약으로 일상생활 지원이 필요한 대상으로 분류된 군이다.
_CARE_PROGRAM_LEVEL = {
    "중점돌봄군": "휠체어·부축 동행",
    "일반돌봄군": "차량+동행",
}


@dataclass
class NeedResult:
    level: str                         # 후보 등급
    score: int = 0                     # 관찰 특성 경로에서만 의미가 있다
    reasons: list[str] = field(default_factory=list)
    guardian_contact: bool = False     # 보호자 연락 필요 여부
    basis: str = BASIS_NONE            # 이 후보를 무엇에 근거해 냈는가
    official: bool = False             # 공식 판정을 옮긴 것인가(True) 우리 추정인가(False)

    def to_dict(self) -> dict:
        return {"level": self.level, "reasons": self.reasons,
                "basis": self.basis, "official": self.official,
                "guardian_contact": self.guardian_contact}


def _normalize_grade(value) -> str | None:
    """'2등급', 2, '인지지원등급' → '2' / '인지지원'. 못 읽으면 None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if "인지지원" in s:
        return "인지지원"
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits if digits in _LTCI_LEVEL else None


def _guardian_note(profile: dict, reasons: list[str]) -> bool:
    """보호자 부재는 어느 경로에서든 동행 매니저 배정의 근거가 된다."""
    if profile.get("guardian") is None:
        reasons.append("보호자 없음 — 동행 매니저 필수")
        return True
    return False


def _observed(profile: dict) -> tuple[int, list[str]]:
    """등록된 관찰 특성 — 거동·낙상·독거.

    공식 등급이 있어도 이 목록은 계속 만든다. 등급은 '어느 수준으로 동행할지'를
    정하지만, 낙상 위험이나 독거는 '동행을 어떻게 할지'를 바꾼다 — 매니저가
    현장에서 알아야 하는 정보라 등급에 가려지면 안 된다.
    점수는 공식 등급이 없을 때만 등급 산정에 쓰인다.
    """
    score, reasons = 0, []
    mobility = profile.get("mobility") or ""
    if "휠체어" in mobility:
        score += 3
        reasons.append("휠체어 필요(등록 이동 특성)")
    elif "거동 불편" in mobility or "보행기" in mobility:
        score += 2
        reasons.append("거동 불편(등록 이동 특성)")
    elif "차량" in mobility:
        score += 1
        reasons.append("장거리 이동 시 차량 필요(등록 이동 특성)")

    if profile.get("fall_risk"):
        score += 1
        reasons.append("낙상 위험")
    if profile.get("lives_alone"):
        score += 1
        reasons.append("독거")
    return score, reasons


def assess(profile: dict | None) -> NeedResult:
    if not profile:
        return NeedResult(
            level="확인 필요",
            reasons=["신규 대상자 — 이동 특성 파악 후 사회복지사가 등급 결정"],
            guardian_contact=False, basis=BASIS_NONE)

    reasons: list[str] = []
    score, observed = _observed(profile)

    # ── 1순위: 장기요양등급 ────────────────────────────────────────
    grade = _normalize_grade(profile.get("ltci_grade"))
    if grade:
        label = "인지지원등급" if grade == "인지지원" else f"장기요양 {grade}등급"
        reasons.append(f"{label} 판정 — 국민건강보험공단 장기요양인정 결과")
        if grade == "인지지원":
            reasons.append("인지 저하 동반 — 이동 보조보다 동반·안내와 보호자 연락이 중요")
        elif grade in ("1", "2"):
            reasons.append("일상생활 전반에 상시 도움이 필요한 구간")
        reasons += observed          # 등급이 정해도 현장 주의사항은 그대로 전달한다
        guardian_contact = _guardian_note(profile, reasons)
        return NeedResult(level=_LTCI_LEVEL[grade], score=score, reasons=reasons,
                          guardian_contact=guardian_contact,
                          basis=BASIS_LTCI, official=True)

    # ── 2순위: 노인맞춤돌봄서비스 군 ────────────────────────────────
    program = (profile.get("care_program") or "").strip()
    if program in _CARE_PROGRAM_LEVEL:
        reasons.append(f"노인맞춤돌봄서비스 {program} 분류 — 지자체 대상자 선정 결과")
        reasons += observed
        guardian_contact = _guardian_note(profile, reasons)
        return NeedResult(level=_CARE_PROGRAM_LEVEL[program], score=score, reasons=reasons,
                          guardian_contact=guardian_contact,
                          basis=BASIS_CARE_PROGRAM, official=True)

    # ── 3순위: 관찰 특성 ───────────────────────────────────────────
    # 여기서부터는 우리 추정이다. 점수는 공식 기준이 아니라 내부 정렬용이며,
    # 결과에 '공식 등급 미확인'을 반드시 붙인다.
    reasons += observed
    guardian_contact = _guardian_note(profile, reasons)
    if guardian_contact:
        score += 1

    level = ("휠체어·부축 동행" if score >= 4
             else "차량+동행" if score >= 2
             else "단순 안내")
    reasons.append("장기요양등급·돌봄군 미등록 — 관찰 특성 기반 임시 후보, 확정 전 공식 등급 확인 필요")
    return NeedResult(level=level, score=score, reasons=reasons,
                      guardian_contact=guardian_contact,
                      basis=BASIS_OBSERVED, official=False)
