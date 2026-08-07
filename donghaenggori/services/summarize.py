"""사후기록 요약 — 동행 매니저 음성 메모 → 기록 초안 (화면 05 / 파일2 STEP 6).

md 6-1 'AI 기술: 요약' 항목. 매니저의 자유 발화 메모에서 5개 항목을 구조화한다.
  진료 내용 · 다음 진료 · 약국 방문 · 다음 동행 주의사항 · 보호자 공유 메시지 초안
  + 케어 프로필 업데이트 제안

설계 원칙
  · AI는 케어 프로필을 자동 변경하지 않는다 — 제안만 하고 승인은 사회복지사가 한다.
  · 상대 날짜("2주 뒤")는 확정하지 않고 '일정 재확인 필요'로 분리한다(파일3 케이스 12 보완사항).
  · ANTHROPIC_API_KEY가 없으면 규칙 기반으로 동작한다 — 발표 때 키 문제로 막히지 않게.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..config import settings

MODEL = settings.anthropic_model

# 상대 날짜 표현 — 확정하지 않고 재확인 항목으로 분리
_REL_DATE = re.compile(r"(\d+\s*(?:주|개월|달|일)\s*(?:뒤|후))")
_PHARMACY = ("약국", "약 받", "약 타", "처방")
_STAIRS = ("계단", "층계")


@dataclass
class PostDraft:
    treatment: str | None = None          # 진료 내용
    next_visit: str | None = None         # 다음 진료
    pharmacy: str | None = None           # 약국 방문
    cautions: str | None = None           # 다음 동행 주의사항
    guardian_msg: str | None = None       # 보호자 공유 메시지 초안
    profile_update: str | None = None     # 케어 프로필 업데이트 제안
    needs_schedule_check: bool = False    # 상대 날짜 → 일정 재확인 필요
    source: str = "규칙"                  # 규칙 | 규칙+LLM
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "treatment": self.treatment, "next_visit": self.next_visit,
            "pharmacy": self.pharmacy, "cautions": self.cautions,
            "guardian_msg": self.guardian_msg, "profile_update": self.profile_update,
        }


# ---------------------------------------------------------------- 규칙 기반 --

def _rule_based(memo: str, target: str | None, dept: str | None) -> PostDraft:
    d = PostDraft()
    text = memo.strip()

    # 진료 내용 — 첫 문장을 근거로, 매니저 진술임을 명시(AI가 의료 판단하지 않음)
    first = re.split(r"[.。\n]", text)[0].strip()
    if first:
        d.treatment = f"{first} (매니저 진술 기준)"

    # 다음 진료 — 상대 날짜는 확정하지 않는다
    m = _REL_DATE.search(text)
    if m:
        d.next_visit = f"약 {m.group(1)}"
        d.needs_schedule_check = True
        d.notes.append("상대 날짜는 확정하지 않고 일정 재확인 항목으로 분리")

    if any(k in text for k in _PHARMACY):
        d.pharmacy = "완료"

    if any(k in text for k in _STAIRS):
        d.cautions = "계단 이동 곤란 · 엘리베이터 동선 확인"
        d.profile_update = "이동 특성에 '계단 이동 곤란' 강화"

    name = (target or "어르신").split("(")[0].strip()
    dept_part = f"{dept} " if dept else ""
    d.guardian_msg = f"오늘 {dept_part}동행 잘 마쳤습니다."
    return d


# ----------------------------------------------------------------- LLM 보강 --

def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _llm_refine(memo: str, target: str | None, dept: str | None, base: PostDraft) -> PostDraft:
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        base.notes.append("anthropic/pydantic 미설치 — 규칙 결과 사용")
        return base

    class Parsed(BaseModel):
        treatment: str | None       # 진료 내용 (매니저 진술 기준으로만)
        next_visit: str | None      # 다음 진료 시점 (원문 표현 그대로, 날짜 확정 금지)
        pharmacy: str | None        # 약국 방문 여부 ("완료" 또는 null)
        cautions: str | None        # 다음 동행 시 주의사항
        guardian_msg: str | None    # 보호자에게 보낼 짧은 공유 메시지 초안
        profile_update: str | None  # 케어 프로필에 반영 제안할 항목 (없으면 null)

    system = (
        "너는 노인 병원동행 서비스의 사후기록 정리 보조다. 동행 매니저가 남긴 음성 메모에서 "
        "사회복지사가 검토할 기록 초안을 만든다.\n"
        "규칙:\n"
        "1) 의료적 판단·진단·복약 지시를 하지 마라. 매니저가 말한 사실만 정리한다.\n"
        "2) '2주 뒤' 같은 상대 날짜를 실제 날짜로 확정하지 마라. 원문 표현 그대로 둔다.\n"
        "3) 메모에 없는 내용을 지어내지 마라. 근거가 없으면 null.\n"
        "4) 보호자 메시지는 2문장 이내, 정중하고 담백하게."
    )
    user = f"대상자: {target or '미상'} / 진료과: {dept or '미상'}\n매니저 음성 메모:\n\"{memo}\""

    try:
        client = anthropic.Anthropic()
        resp = client.messages.parse(
            model=MODEL, max_tokens=1024, system=system,
            messages=[{"role": "user", "content": user}],
            output_format=Parsed,
        )
        p = resp.parsed_output
        if p is None:
            base.notes.append("LLM 파싱 실패 — 규칙 결과 사용")
            return base
        return PostDraft(
            treatment=p.treatment or base.treatment,
            next_visit=p.next_visit or base.next_visit,
            pharmacy=p.pharmacy or base.pharmacy,
            cautions=p.cautions or base.cautions,
            guardian_msg=p.guardian_msg or base.guardian_msg,
            profile_update=p.profile_update or base.profile_update,
            needs_schedule_check=base.needs_schedule_check or bool(_REL_DATE.search(memo)),
            source="규칙+LLM",
            notes=base.notes,
        )
    except Exception as e:
        base.notes.append(f"LLM 오류로 규칙 결과 사용: {type(e).__name__}")
        return base


# ------------------------------------------------------------------ 공개 API --

def summarize(memo: str, target: str | None = None, dept: str | None = None,
              use_llm: bool | None = None) -> PostDraft:
    """음성 메모 → 사후기록 초안. 항상 '검토 필요' 상태로 사람에게 넘긴다."""
    base = _rule_based(memo, target, dept)
    if use_llm is None:
        use_llm = _llm_available()
    return _llm_refine(memo, target, dept, base) if use_llm else base
