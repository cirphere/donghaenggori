"""사후기록 요약 — 동행 매니저 음성 메모 → 기록 초안 (화면 05 / 파일2 STEP 6).

md 6-1 'AI 기술: 요약' 항목. 매니저의 자유 발화 메모에서 5개 항목을 구조화한다.
  진료 내용 · 다음 진료 · 약국 방문 · 다음 동행 주의사항 · 보호자 공유 메시지 초안
  + 케어 프로필 업데이트 제안

설계 원칙
  · AI는 케어 프로필을 자동 변경하지 않는다 — 제안만 하고 승인은 사회복지사가 한다.
  · 상대 날짜("2주 뒤")는 확정하지 않고 '일정 재확인 필요'로 분리한다(파일3 케이스 12 보완사항).
  · ANTHROPIC_API_KEY가 없으면 규칙 기반으로 동작한다 — 발표 때 키 문제로 막히지 않게.

키 없이 개발·검증하는 법
    python -m donghaenggori.services.summarize
  규칙 결과 → 실제로 보낼 요청(모델·시스템·스키마) → 스텁 클라이언트 왕복까지
  키 없이 전부 확인한다. 키가 .env에 들어오는 순간 같은 경로가 그대로 켜진다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..config import settings

MODEL = settings.anthropic_model
MAX_TOKENS = 4096          # Opus 5는 사고 토큰과 응답이 이 한도를 함께 쓴다 — 여유를 준다
EFFORT = "low"             # 짧은 메모 구조화. 시연 지연을 줄이는 주 조절 손잡이

# 상대 날짜 표현 — 확정하지 않고 재확인 항목으로 분리
_REL_DATE = re.compile(r"(\d+\s*(?:주|개월|달|일)\s*(?:뒤|후))")
# 진료 내용 절이 여기서 끝난다 — 뒤는 '다음 진료' 항목으로 넘긴다
_NEXT_VISIT = re.compile(r"(다음\s*(?:진료|방문|예약|외래)|다음번|재진)")
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

def _clause(s: str) -> str:
    return s.strip().strip(",·;、").strip()


def _treatment_clause(text: str) -> str | None:
    """첫 문장에서 '진료 내용'에 해당하는 앞부분만 잘라낸다.

    문장 전체를 넣으면 다음 진료·약국 정보까지 진료 내용에 딸려 들어가
    항목이 중복된다. 뒤따르는 절은 각자의 항목이 가져간다.
    """
    first = re.split(r"[.。\n]", text)[0]
    cuts = [m.start() for m in (_NEXT_VISIT.search(first),) if m]
    pm = re.search("|".join(_PHARMACY), first)
    if pm:
        cuts.append(pm.start())
    if cuts:
        first = first[:min(cuts)]
    return _clause(first) or None


def _rule_based(memo: str, target: str | None, dept: str | None) -> PostDraft:
    d = PostDraft()
    text = memo.strip()

    # 진료 내용 — 매니저 진술임을 명시한다(AI가 의료 판단하지 않음)
    t = _treatment_clause(text)
    if t:
        d.treatment = f"{t} (매니저 진술 기준)"

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

    dept_part = f"{dept} " if dept else ""
    d.guardian_msg = f"오늘 {dept_part}동행 잘 마쳤습니다."
    return d


# ----------------------------------------------------------------- LLM 보강 --

_SYSTEM = (
    "너는 노인 병원동행 서비스의 사후기록 정리 보조다. 동행 매니저가 남긴 음성 메모에서 "
    "사회복지사가 검토할 기록 초안을 만든다.\n"
    "규칙:\n"
    "1) 의료적 판단·진단·복약 지시를 하지 마라. 매니저가 말한 사실만 정리한다.\n"
    "2) '2주 뒤' 같은 상대 날짜를 실제 날짜로 확정하지 마라. 원문 표현 그대로 둔다.\n"
    "3) 메모에 없는 내용을 지어내지 마라. 근거가 없으면 null.\n"
    "4) 보호자 메시지는 2문장 이내, 정중하고 담백하게.\n"
    "5) 진료 내용과 다음 진료를 한 항목에 섞지 마라. 각각의 칸에 나눠 담는다."
)


def _user_prompt(memo: str, target: str | None, dept: str | None) -> str:
    return (f"대상자: {target or '미상'} / 진료과: {dept or '미상'}\n"
            f"매니저 음성 메모:\n\"{memo}\"")


def _schema_model():
    """출력 스키마. 설명은 Field에 담아야 모델에게 실제로 전달된다."""
    from pydantic import BaseModel, Field

    class Parsed(BaseModel):
        treatment: str | None = Field(
            description="진료 내용. 매니저가 말한 사실만. 다음 진료 일정은 넣지 않는다.")
        next_visit: str | None = Field(
            description="다음 진료 시점. 원문 표현 그대로(예: '2주 뒤'). 날짜 확정 금지.")
        pharmacy: str | None = Field(
            description="약국 방문 여부. 다녀왔으면 '완료', 언급이 없으면 null.")
        cautions: str | None = Field(
            description="다음 동행 때 매니저가 알아야 할 주의사항.")
        guardian_msg: str | None = Field(
            description="보호자에게 보낼 짧은 공유 메시지 초안. 2문장 이내.")
        profile_update: str | None = Field(
            description="케어 프로필에 반영을 '제안'할 항목. 없으면 null.")

    return Parsed


def request_preview(memo: str, target: str | None = None,
                    dept: str | None = None) -> dict:
    """실제로 보낼 요청을 그대로 돌려준다 — 키 없이 프롬프트·스키마를 점검하는 용도."""
    Parsed = _schema_model()
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": _user_prompt(memo, target, dept)}],
        "output_format": Parsed.__name__,
        "schema": Parsed.model_json_schema(),
    }


def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _llm_refine(memo: str, target: str | None, dept: str | None,
                base: PostDraft, client=None) -> PostDraft:
    """LLM으로 항목을 보강한다. 실패하면 언제나 규칙 결과로 되돌아간다.

    client를 주입하면 키 없이도 이 경로를 그대로 실행·검증할 수 있다.
    """
    try:
        Parsed = _schema_model()
    except ImportError:
        base.notes.append("pydantic 미설치 — 규칙 결과 사용")
        return base

    if client is None:
        try:
            import anthropic
        except ImportError:
            base.notes.append("anthropic 미설치 — 규칙 결과 사용")
            return base
        client = anthropic.Anthropic(timeout=settings.anthropic_timeout)

    try:
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # Opus 5는 사고가 기본으로 켜져 있다. 끄는 대신 effort를 낮춰
            # 비용·지연을 줄인다(끄면 도구·태그 관련 부작용이 알려져 있다).
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            system=_SYSTEM,
            messages=[{"role": "user", "content": _user_prompt(memo, target, dept)}],
            output_format=Parsed,
        )
    except Exception as e:
        base.notes.append(f"LLM 오류로 규칙 결과 사용: {type(e).__name__}")
        return base

    # 안전 분류기가 거부하면 200이지만 본문이 비어 있다 — 읽기 전에 확인한다
    if getattr(resp, "stop_reason", None) == "refusal":
        base.notes.append("LLM이 응답을 거부함 — 규칙 결과 사용")
        return base

    p = getattr(resp, "parsed_output", None)
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
        # 상대 날짜 판정은 LLM에 맡기지 않는다 — 규칙이 최종 권한
        needs_schedule_check=base.needs_schedule_check or bool(_REL_DATE.search(memo)),
        source="규칙+LLM",
        notes=base.notes,
    )


# ------------------------------------------------------------------ 공개 API --

def summarize(memo: str, target: str | None = None, dept: str | None = None,
              use_llm: bool | None = None, client=None) -> PostDraft:
    """음성 메모 → 사후기록 초안. 항상 '검토 필요' 상태로 사람에게 넘긴다."""
    base = _rule_based(memo, target, dept)
    if use_llm is None:
        use_llm = client is not None or _llm_available()
    if not use_llm:
        return base
    return _llm_refine(memo, target, dept, base, client=client)


# --------------------------------------------------------- 키 없이 점검하기 --

class StubClient:
    """LLM 경로를 키 없이 실행해 보기 위한 스텁. 고정된 결과를 돌려준다."""

    def __init__(self, **fields):
        self.messages = self          # client.messages.parse(...) 형태를 흉내낸다
        self.fields = fields
        self.last_request: dict = {}

    def parse(self, **kw):
        self.last_request = kw
        model = kw["output_format"]
        data = {n: self.fields.get(n) for n in model.model_fields}
        return type("Resp", (), {"parsed_output": model(**data),
                                 "stop_reason": "end_turn"})()


def _selftest() -> None:
    MEMO = "오늘 무릎 주사 맞았고, 다음 진료는 2주 뒤. 약국 들러서 약 받았어요. 계단 힘들어하셨습니다."
    kw = dict(target="박순자 어르신", dept="정형외과")

    print("① 규칙 기반 (키 없이 항상 동작)")
    for k, v in summarize(MEMO, **kw, use_llm=False).as_dict().items():
        print(f"   {k:<15} {v}")

    print("\n② 실제로 보낼 요청 (키 없이 점검)")
    req = request_preview(MEMO, **kw)
    for k in ("model", "max_tokens", "thinking", "output_config"):
        print(f"   {k:<15} {req[k]}")
    print(f"   {'schema 필드':<15} {list(req['schema']['properties'])}")

    print("\n③ LLM 경로 왕복 (스텁 클라이언트 주입)")
    stub = StubClient(treatment="무릎 관절 주사 시술", next_visit="2주 뒤",
                      pharmacy="완료", cautions="계단 이동 곤란 — 엘리베이터 동선 확인",
                      guardian_msg="오늘 정형외과 진료 잘 마치셨습니다. 약도 받아 두었습니다.",
                      profile_update="이동 특성에 '계단 이동 곤란' 강화")
    d = summarize(MEMO, **kw, client=stub)
    for k, v in d.as_dict().items():
        print(f"   {k:<15} {v}")
    print(f"   {'source':<15} {d.source}")
    print(f"   {'일정 재확인':<15} {d.needs_schedule_check}")

    assert d.source == "규칙+LLM", "LLM 경로가 실행되지 않았다"
    assert d.needs_schedule_check, "상대 날짜 판정은 규칙이 최종 권한"
    assert stub.last_request["model"] == MODEL
    assert stub.last_request["output_config"] == {"effort": EFFORT}

    print("\n④ 실패 시 폴백 (LLM이 예외를 던지는 상황)")
    class Boom:
        def __init__(self):
            self.messages = self
        def parse(self, **kw):
            raise RuntimeError("연결 실패")
    f = summarize(MEMO, **kw, client=Boom())
    print(f"   source={f.source} · notes={f.notes}")
    assert f.source == "규칙", "실패했는데 LLM 결과로 표시되면 안 된다"
    assert f.treatment, "폴백 결과가 비어 있다"

    print(f"\n키 없이 4개 경로 모두 통과. ANTHROPIC_API_KEY를 .env에 넣으면 "
          f"{MODEL} 로 자동 전환됩니다.")
    print(f"현재 키 상태: {settings.status()['ANTHROPIC_API_KEY']}")


if __name__ == "__main__":
    _selftest()
