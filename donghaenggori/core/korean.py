"""한국어 조사 처리.

접수카드의 근거·확인 질문은 값을 문장에 끼워 만든다. 그런데 한국어 조사는
앞 글자의 받침에 따라 갈린다 — "3시**가**" 인데 "30분**이**" 다. 문자열을
그대로 이어 붙이면 "3시이 오전인가요", "방문 날짜을 확인할 수 없음" 같은
문장이 사회복지사 화면에 뜬다.

문구를 조사가 없는 형태로 우회할 수도 있지만, 그러다 보면 "어르신이 직접
말한 표현: '3시'" 처럼 어색해진다. 실제로 같은 실수를 네 번 반복해서
(이/가 · 은/는 · 을/를 · 이라고/라고) 여기로 모았다.

숫자로 끝나는 값도 흔해서(2026-08-14, 10시 30분) 숫자 발음의 받침까지 본다.
"""
from __future__ import annotations

# 받침이 있는 것으로 읽히는 숫자 — 일(ㄹ) 삼(ㅁ) 육(ㄱ) 칠(ㄹ) 팔(ㄹ) 십(ㅂ)
_DIGIT_HAS_FINAL = {"0": True, "1": True, "3": True, "6": True, "7": True, "8": True,
                    "2": False, "4": False, "5": False, "9": False}

_PAIRS = {
    "이": ("이", "가"), "가": ("이", "가"),
    "은": ("은", "는"), "는": ("은", "는"),
    "을": ("을", "를"), "를": ("을", "를"),
    "과": ("과", "와"), "와": ("과", "와"),
    "이라고": ("이라고", "라고"), "라고": ("이라고", "라고"),
    "으로": ("으로", "로"), "로": ("으로", "로"),
}


def has_final(word: str) -> bool:
    """마지막 글자에 받침이 있는가. 판단할 수 없으면 False."""
    if not word:
        return False
    ch = word.strip()[-1] if word.strip() else ""
    if not ch:
        return False
    if ch.isdigit():
        return _DIGIT_HAS_FINAL.get(ch, False)
    code = ord(ch)
    if 0xAC00 <= code <= 0xD7A3:            # 한글 음절
        return (code - 0xAC00) % 28 != 0
    return False                            # 영문·기호는 받침 없는 쪽으로


def particle(word: str, kind: str) -> str:
    """조사만 돌려준다. 값이 따옴표 안에 들어갈 때 쓴다.

        f"'{label}'{particle(label, '이라고')}"  →  '모레'라고 / '10시'라고
                                                    '다음주 화요일'이라고

    조사는 앞 글자의 **발음**을 따르므로 닫는 따옴표는 계산에서 빼야 한다.
    """
    pair = _PAIRS.get(kind)
    if pair is None:
        return kind
    return pair[0] if has_final(word) else pair[1]


def josa(word: str, particle_kind: str) -> str:
    """단어에 맞는 조사를 붙여 돌려준다.

        josa("3시", "가")    → "3시가"
        josa("30분", "가")   → "30분이"
        josa("날짜", "을")   → "날짜를"
        josa("시각", "을")   → "시각을"

    'ㄹ' 받침은 '으로/로' 에서 예외다 — "서울로" 이지 "서울으로" 가 아니다.
    """
    pair = _PAIRS.get(particle_kind)
    if pair is None:
        return f"{word}{particle_kind}"
    with_final, without_final = pair
    if particle_kind in ("으로", "로"):
        ch = (word.strip() or " ")[-1]
        if not ch.isdigit() and 0xAC00 <= ord(ch) <= 0xD7A3 and (ord(ch) - 0xAC00) % 28 == 8:
            return f"{word}{without_final}"   # ㄹ 받침 → '로'
    return f"{word}{with_final if has_final(word) else without_final}"
