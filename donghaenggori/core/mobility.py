"""이동지원 필요 여부와 보호자 언급 — **발화에서만** 뽑는다.

성능평가로 확인된 것: 카드의 `need_level`(지원 수준)과 `guardian`(연락처)은
**발화가 아니라 케어 프로필에서 온다.** 그래서 어르신이 "나 혼자 갈 수 있어"
라고 말해도 카드는 프로필의 등급으로 "휠체어·부축 동행" 을 내놓았고, 반대로
"사람이 필요해" 라고 말해도 그 말이 카드에 남지 않았다.

두 값을 **섞지 않고 나란히** 둔다.

    지원 수준(프로필 기반)   need_level        장기요양등급·거동 특성 → 배차 판단
    이번 통화 언급(발화 기반) mobility_need     이 통화에서 어르신이 말한 것

출처를 나눠 보여주는 것이 이 서비스의 강점과 맞는다 — 무엇을 근거로 판단했는지
화면에서 갈라 보이는 것이 곧 설명가능성이다.

## 이 모듈이 보지 않는 것

**프로필·이력·DB 를 보지 않는다.** 입력은 발화 텍스트뿐이다. 한 번 섞이면
"발화에서 뽑았는지 프로필에서 왔는지" 를 다시 구분할 수 없게 되고, 그게 바로
이번에 고치는 문제였다.

## 규칙 기반이다

확정/추정/확인필요를 LLM 에게 묻지 않는다. 판정은 발화에 있는 말로 하고,
근거는 **원문 문구 그대로** 남긴다(core/requesttype.py 와 같은 방식).

방언 표현은 팀 사전(`docs/eval/전남방언_매핑사전.xlsx`)의 항목을 따랐다 —
'혼자 가긴 좀 그런디'(이동지원 필요 암시), '삭신이 쑤신다', '우리 아들내미',
'~는디/~것다' 어미가 거기서 왔다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

# ── 판정값 ───────────────────────────────────────────────────────────────
EXPLICIT_NEED = "명시적_필요"
EXPLICIT_NO_NEED = "명시적_불필요"
FAMILY_SUPPORT = "가족지원있음"
IMPLICIT_NEED = "암시적_필요"
NO_SIGNAL = "신호없음"

VERDICTS = (EXPLICIT_NEED, EXPLICIT_NO_NEED, FAMILY_SUPPORT, IMPLICIT_NEED, NO_SIGNAL)

# 판정 → (필요 여부, 상태). '신호없음' 은 값을 만들지 않는다.
#
# **가족지원있음을 명시적_불필요와 따로 둔 이유.** 둘 다 '불필요·확정' 이지만
# 근거가 다르다 — 어르신이 스스로 괜찮다고 한 것과, 가족이 데려다주기로 한
# 것은 다른 상황이다. 가족이 못 오게 되면 **바로 필요해진다.** 복지사가 그
# 차이를 알아야 하므로 판정값으로 갈라 두고 근거에 남긴다.
_DECISION = {
    EXPLICIT_NEED:    ("필요", "확인됨"),
    EXPLICIT_NO_NEED: ("불필요", "확인됨"),
    FAMILY_SUPPORT:   ("불필요", "확인됨"),
    IMPLICIT_NEED:    ("필요", "추정"),
    NO_SIGNAL:        (None, "확인 필요"),
}

# ── 신호 사전 ────────────────────────────────────────────────────────────
#
# 전부 원문에 그대로 있는 문자열이다. 매칭된 문구를 근거로 싣기 때문에
# 정규식으로 넓게 잡지 않고 실제 말투를 적는다.

# 사람이 필요하다고 **직접** 말한 것
_NEED = (
    "사람 좀 보내", "사람을 보내", "사람 보내", "사람이 필요", "사람이 있어야",
    "도움이 필요", "도와주셔야", "도와줘야", "같이 가 주", "같이 가주",
    "같이 가실", "같이 가 주실", "데려다 주", "데려다주", "델러다 주",
    "모시고 가 주", "동행", "부축", "휠체어",
)

# 도움이 필요 없다고 **직접** 말한 것
_NO_NEED = (
    "혼자 갈 수 있", "혼자 갈수 있", "혼자 갈 수 있어", "혼자 가면 돼",
    "혼자 가도 되", "혼자 갈라", "도움 필요없", "도움 필요 없", "도움은 필요없",
    "안 도와줘도", "괜찮아요 혼자", "혼자서 갈", "내가 알아서",
)

# 가족이 데려다준다 — 우리 동행은 불필요하지만 **근거가 다르다.**
# '우리 아들내미' 는 팀 사전의 '가족/호칭' 항목이다.
_FAMILY = (
    "아들내미가 델러다", "아들이 데려다", "아들이 델러다", "딸이 데려다",
    "딸이 델러다", "며느리가 데려다", "사위가 데려다", "손자가 데려다",
    "아들내미가 데려다", "가족이 데려다", "식구가 데려다", "아들이 태워",
    "딸이 태워", "아들내미가 태워", "우리 아들내미가", "우리 아들이",
    "우리 딸이", "애들이 데려다", "애들이 태워",
)

# 직접 말하지 않았지만 추론할 수 있는 것.
# '혼자 가긴 좀 그런디' 는 팀 사전의 '이동/지원 암시' 항목이다.
_IMPLICIT = (
    "혼자 가긴 좀", "혼자 가기 좀", "혼자 가기가 좀", "혼자 가긴 그", "혼자 가기 그",
    "혼자 가긴 어렵", "혼자 가기 어렵", "혼자는 못", "혼자 못 가", "혼자 갈 수가 없",
    "혼자서는 못", "버스 타고 혼자", "걸어가기 힘들", "걷기가 힘들",
    "다리가 불편", "다리가 아파서 못", "삭신이 다 쑤셔", "삭신이 쑤셔",
    "몸이 안 좋아서 못", "못 가것다", "못 가겠",
    "가족들은 멀리", "가족이 멀리", "자식들은 멀리", "애들은 멀리",
    "혼자 살아", "혼자 사는", "돌봐 줄 사람이 없", "봐 줄 사람이 없",
)

# ── 보호자 언급 ─────────────────────────────────────────────────────────
MENTIONED = "언급있음"

_GUARDIAN_WORDS = ("아들내미", "아들", "딸", "며느리", "사위", "손자", "손녀",
                   "가족", "식구", "자식", "애들", "남편", "영감", "할멈",
                   "보호자", "조카", "형제", "동생")

# 보호자를 말했지만 **도움을 받을 수 없다**는 맥락. 있음/없음을 갈라야 한다 —
# "가족들은 멀리 살고" 를 '보호자 있음' 으로 적으면 복지사가 연락하려 한다.
_ABSENT = ("멀리 살", "멀리 있", "멀리 사", "연락이 안", "없어", "없다", "없는",
           "안 계셔", "안 계신", "돌아가셨", "바빠서 못", "못 와", "못 온",
           "올 수 없", "도움을 못")


@dataclass
class MobilityNeed:
    """발화 기반 이동지원 판정. **프로필 값이 섞이지 않는다.**"""

    verdict: str = NO_SIGNAL
    need: str | None = None            # 필요 | 불필요 | None
    status: str = "확인 필요"           # 확인됨 | 추정 | 확인 필요
    evidence: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return {"판정": self.verdict, "필요여부": self.need,
                "상태": self.status, "근거문구": self.evidence}


@dataclass
class GuardianMention:
    """발화 기반 보호자 언급. 연락처(card.guardian)와 다른 것이다."""

    verdict: str = NO_SIGNAL
    content: str | None = None
    status: str = "확인 필요"
    evidence: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return {"판정": self.verdict, "내용": self.content,
                "상태": self.status, "근거문구": self.evidence}


def _hits(text: str, words) -> list[str]:
    """원문에 실제로 있는 문구만, 긴 것부터. 돌려주는 것이 곧 근거다."""
    found = [w for w in words if w in text]
    found.sort(key=len, reverse=True)
    return found


def extract_mobility_need(utterance: str) -> MobilityNeed:
    """발화 하나에서 이동지원 필요 여부를 가른다. **입력은 텍스트뿐이다.**

    순서가 정해져 있다.
      ① 명시적 불필요 — 어르신이 스스로 괜찮다고 한 말이 가장 강하다
      ② 가족지원있음 — 우리 동행은 불필요하지만 근거가 다르다
      ③ 명시적 필요
      ④ 암시적 필요
      ⑤ 신호없음

    ①을 먼저 보는 이유: "혼자 갈 수 있어" 와 "다리가 불편" 이 한 문장에 같이
    올 수 있다("다리가 불편하지만 혼자 갈 수 있어"). 그때 어르신이 직접 밝힌
    쪽을 따른다 — 우리 추론이 당사자의 말을 덮으면 안 된다.
    """
    text = utterance or ""

    no_need = _hits(text, _NO_NEED)
    if no_need:
        return _build(EXPLICIT_NO_NEED, no_need,
                      "어르신이 직접 도움이 필요 없다고 말함")

    family = _hits(text, _FAMILY)
    if family:
        return _build(FAMILY_SUPPORT, family,
                      "가족이 데려다주기로 함 — 우리 동행은 불필요하나, "
                      "가족이 못 오게 되면 바로 필요해진다(어르신 본인이 "
                      "괜찮다고 한 것과는 다르다)")

    need = _hits(text, _NEED)
    if need:
        return _build(EXPLICIT_NEED, need, "어르신이 직접 도움을 요청함")

    implicit = _hits(text, _IMPLICIT)
    if implicit:
        return _build(IMPLICIT_NEED, implicit,
                      "직접 말하지는 않았으나 이동이 어렵다는 표현 — 추정이므로 "
                      "확정 전 사회복지사 확인")

    return MobilityNeed(NO_SIGNAL, None, "확인 필요",
                        ["발화에 이동지원 관련 언급이 없음 — "
                         "프로필의 지원 수준을 이 칸에 쓰지 않는다"])


def _build(verdict: str, hits: list[str], why: str) -> MobilityNeed:
    need, status = _DECISION[verdict]
    ev = [f"원문 '{h}'" for h in hits[:3]] + [why]
    return MobilityNeed(verdict, need, status, ev)


def extract_guardian_info(utterance: str) -> GuardianMention:
    """발화에서 보호자 관련 언급을 뽑는다. **연락처를 만들지 않는다.**

    카드의 guardian(이름·관계·연락처)은 프로필에서 오는 실무 정보이고, 이건
    "이번 통화에서 보호자를 어떻게 말했나" 다. 둘을 한 칸에 담으면 못 만났을 때
    전화할 번호와 통화에서 들은 이야기가 섞인다.
    """
    text = utterance or ""
    words = _hits(text, _GUARDIAN_WORDS)
    if not words:
        return GuardianMention(NO_SIGNAL, None, "확인 필요",
                               ["발화에 보호자 관련 언급이 없음 — "
                                "프로필의 보호자 연락처를 이 칸에 쓰지 않는다"])

    who = words[0]
    absent = _hits(text, _ABSENT)
    helping = _hits(text, _FAMILY)

    if helping:
        content = f"{who} 이(가) 데려다줄 수 있음"
        why = "가족이 동행을 대신함 — 연락처는 프로필에서 확인"
    elif absent:
        content = f"{who} 있으나 도움을 받기 어려움 ({absent[0]})"
        why = "보호자를 말했지만 도움을 받을 수 없는 맥락 — 연락 대상으로 쓰지 말 것"
    else:
        content = f"{who} 을(를) 언급함"
        why = "언급만 있어 도움 가능 여부는 알 수 없음 — 사회복지사 확인"

    ev = [f"원문 '{w}'" for w in words[:2]]
    if absent:
        ev.append(f"원문 '{absent[0]}'")
    ev.append(why)
    # 언급이 있었다는 것까지가 확인된 사실이다. 그 내용(도움 가능 여부)은
    # 확정하지 않는다 — 통화 한 마디로 가족 사정을 단정할 수 없다.
    return GuardianMention(MENTIONED, content, "확인됨", ev)


_SPACE = re.compile(r"\s+")


def summary(m: MobilityNeed) -> str:
    """카드 한 줄. 판정과 상태를 함께 읽히게 한다."""
    if m.need is None:
        return "확인 필요 (이번 통화에 언급 없음)"
    return _SPACE.sub(" ", f"{m.need} [{m.status}] · {m.verdict}").strip()
