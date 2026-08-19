"""상대 시간 표현을 실제 날짜로 해석하는 규칙 기반 파서.

계획서 6장: "날짜·상대시간 표현은 규칙 기반 파서로 1차 처리하고 모호 문맥만 LLM이 보완".
어르신 발화의 '모레', '다음주 화요일' 같은 표현을 결정적 규칙으로 처리해 안정성을 확보한다.
"""
from __future__ import annotations

import datetime
import re

_WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_REL_DAYS = {"오늘": 0, "낼": 1, "내일": 1, "모레": 2, "모래": 2, "글피": 3}

# '내일모레 / 낼모레' 의 앞부분. 뒤에 모레·모래가 바로 붙을 때만 떼어낸다.
# (모래는 STT 오인식 — _REL_DAYS 가 이미 같은 날로 본다)
_NAEIL_MORE = re.compile(r"(?:내일|낼)(?=모[레래])")

# 표현이 여러 개 나왔을 때 "마지막 것이 최종"이라고 볼 수 있는 경우는
# **말을 고쳤을 때뿐**이다. 그 외에는 확정하면 안 된다.
#
#   정정   "내일 아니고 모레"        → 모레
#   선택지 "10시나 11시쯤"          → 확인 필요 (11시로 정하면 안 된다)
#   범위   "10시부터 11시 사이"      → 확인 필요
#   부정   "10시는 아니에요"         → 확인 필요
#
# 처음에는 무조건 마지막을 채택했는데, 선택지·범위·부정을 전부 확정으로
# 처리해 버렸다. 어르신은 "화요일이나 수요일" 처럼 말하는 일이 흔하고,
# 그걸 수요일로 확정하면 헛걸음이 난다.
# '안 되고 / 못 가고' 도 정정이다 — "내일은 안 되고 모레로 해줘". 어르신이
# 앞 날짜를 물리는 흔한 말인데 '아니/말고' 만 보다가 놓쳐서 '복수 표현' 으로
# 걸렸다. 공백은 이미 지워진 텍스트에서 찾는다.
#
# **찾는 범위가 두 날짜 표현 사이뿐이라 넓혀도 안전하다.** 거기에 '못' 이
# 있으면 앞 날짜를 물린 것이지 선택지를 늘어놓은 것이 아니다. 선택지·범위는
# '이나·쯤·부터·까지' 로 이어지지 부정어가 끼지 않는다.
_CORRECTION = re.compile(r"아니(?!면)|말고|말구|안되|못|힘들")
# 마지막 표현 **뒤에** 붙는 부정 — "열 시는 아니에요"
_TRAILING_NEGATION = re.compile(r"^\s*(?:는|은|이|가)?\s*아니")


# 확정하지 못한 사유. 확인 질문 문구가 사유마다 달라야 한다 —
# "오전인가요 오후인가요"와 "둘 중 언제신가요"는 다른 질문이다.
AMBIGUOUS_MERIDIEM = "오전·오후 불명"
AMBIGUOUS_MULTIPLE = "복수 표현"
AMBIGUOUS_NEGATED = "부정"


def _resolve(cands: list[tuple[int, object, str]], text: str,
             ends: list[int]) -> tuple[object, str, bool, bool, str | None]:
    """후보들 중 무엇을 채택할지 정한다.

    반환: (값, 표현, corrected, confident, 사유)
    값이 여럿이면 정정으로 이어진 경우만 마지막을 쓰고, 아니면 확정하지 않는다.
    """
    order = sorted(range(len(cands)), key=lambda i: cands[i][0])
    idx = order[-1]
    _, value, label = cands[idx]

    # 마지막 표현 바로 뒤가 부정이면 그 값을 근거로 쓸 수 없다
    if _TRAILING_NEGATION.match(text[ends[idx]:]):
        return None, label, False, False, AMBIGUOUS_NEGATED

    if len({c[1] for c in cands}) <= 1:
        return value, label, False, True, None

    # 값이 둘 이상 — 직전 표현과 이 표현 사이에 정정 표현이 있는지 본다
    between = text[ends[order[-2]]:cands[idx][0]]
    if _CORRECTION.search(between):
        return value, label, True, True, None

    # 선택지·범위 — 어느 쪽인지 우리가 고르면 안 된다. 값을 비우고 들은 표현을
    # 그대로 넘겨서, 확인 질문이 "10시 / 11시 중 언제신가요"가 되게 한다.
    spoken = " / ".join(dict.fromkeys(cands[i][2] for i in order))
    return None, spoken, False, False, AMBIGUOUS_MULTIPLE


def parse_date(text: str, today: datetime.date | None = None) -> dict | None:
    """발화 텍스트에서 날짜 표현을 찾아 해석한다.

    반환: {"date","label","confident","corrected"} 또는 None.
    confident=False면 날짜를 못 찾았거나 애매한 경우 → 접수카드에서 '확인 필요'.
    corrected=True면 앞선 날짜 표현을 말하는 도중에 정정했다는 뜻이다.

    표현을 하나 찾고 곧바로 반환하지 않는다. 어르신은 말하면서 고친다 —
    "내일 아니고 모레 가야 해"에서 먼저 나온 '내일'을 채택하면 정정 이전
    날짜로 접수된다. 그래서 후보를 전부 모아 **말한 순서상 마지막**을 쓴다.
    """
    if today is None:
        today = datetime.date.today()
    t = text.replace(" ", "")          # 위치 비교를 위해 공백 제거본 하나로 통일한다

    # '내일모레' 는 두 날이 아니라 하루다 — 모레를 뜻하는 한 낱말이다.
    #
    # 그대로 두면 '내일' 과 '모레' 가 각각 후보로 잡혀 '복수 표현' 으로
    # 걸리고, 날짜가 비어 확정 게이트가 막힌다. 어르신은 분명하게 하루를
    # 말했는데 확인 전화를 한 통 더 걸게 되는 것이다.
    #
    # 앞의 '내일/낼' 만 떼어낸다. 붙어 있을 때만 — "내일은 안 되고 모레"
    # 처럼 사이에 말이 끼면 그건 진짜 정정이라 기존 규칙이 처리한다.
    t = _NAEIL_MORE.sub("", t)

    cands: list[tuple[int, str, str]] = []      # (위치, 날짜, 원문 표현)
    ends: list[int] = []                        # 각 표현이 끝나는 위치

    # 1) 오늘/내일/모레/글피
    for word, delta in _REL_DAYS.items():
        for m in re.finditer(re.escape(word), t):
            d = today + datetime.timedelta(days=delta)
            cands.append((m.start(), d.isoformat(), word))
            ends.append(m.end())

    # 2) (이번주/다음주/담주) + 요일
    for m in re.finditer(r"(이번주|다음주|담주)?([월화수목금토일])요일", t):
        base_week, wd = m.group(1), _WEEKDAYS[m.group(2)]
        if base_week in ("다음주", "담주"):
            # **다음 주(월요일 시작)의 그 요일**이다. 다가오는 요일에 7 을 더하면
            # 안 된다 — 그 요일이 이번 주에 이미 지났으면 두 주 뒤가 된다.
            #   수요일에 "다음주 화요일" → (화-수)%7=6, +7=13일 뒤 → 두 주 뒤
            # 기준일이 화요일일 때만 우연히 맞아서 오래 안 드러났다. 어르신이
            # 일주일 늦게 병원 앞에 서는 종류의 오류다.
            next_monday = today + datetime.timedelta(days=7 - today.weekday())
            d = next_monday + datetime.timedelta(days=wd)
        else:
            days_ahead = (wd - today.weekday()) % 7
            if days_ahead == 0:  # 요일만 말했고 오늘이면 다음 주기로
                days_ahead = 7
            d = today + datetime.timedelta(days=days_ahead)
        label = (f"{base_week} " if base_week else "") + f"{m.group(2)}요일"
        cands.append((m.start(), d.isoformat(), label))
        ends.append(m.end())

    # 3) N일 뒤/후
    for m in re.finditer(r"(\d+)일(뒤|후)", t):
        d = today + datetime.timedelta(days=int(m.group(1)))
        cands.append((m.start(), d.isoformat(), f"{m.group(1)}일 {m.group(2)}"))
        ends.append(m.end())

    # 4) M월 D일 (연도 없으면 올해, 이미 지났으면 내년)
    for m in re.finditer(r"(\d{1,2})월(\d{1,2})일", t):
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = datetime.date(today.year, month, day)
        except ValueError:
            continue
        if d < today:
            d = datetime.date(today.year + 1, month, day)
        cands.append((m.start(), d.isoformat(), f"{month}월 {day}일"))
        ends.append(m.end())

    if not cands:
        return None

    value, label, corrected, confident, why = _resolve(cands, t, ends)
    return {"date": value, "label": label, "confident": confident,
            "corrected": corrected, "ambiguous": why}


# 오후로 읽어야 하는 말들. '낮 1시'는 13시, '낮 12시'는 12시다.
_PM_WORDS = ("오후", "저녁", "밤", "낮")
_AM_WORDS = ("오전", "아침", "새벽")


# 시(時)를 세는 한국어 고유수사. 어르신 발화에서 "세시" 가 "3시" 만큼 흔한데
# 숫자 정규식만 보던 시절엔 통째로 놓쳐 '확인 필요' 로 떨어졌다 — 안전하긴 해도
# 물어보지 않아도 될 것을 물어보게 만든다.
#
# **'한시'는 넣지 않는다.** '한시간', '한시라도' 처럼 시각이 아닌 쓰임이 흔해
# 오탐이 크다. 못 읽으면 확인 질문이 나갈 뿐이지만, 잘못 읽으면 어르신이
# 엉뚱한 시각에 병원 앞에 선다.
_HOUR_WORDS = {
    "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
    "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
    "열한": 11, "열두": 12,
}
# 긴 것부터 바꿔야 '열'이 '열한'을 먼저 먹지 않는다.
_HOUR_WORD_RE = re.compile(
    "(" + "|".join(sorted(_HOUR_WORDS, key=len, reverse=True)) + r")시")


def _digitize_hours(t: str) -> str:
    """'세시반' → '3시반'. 시각 표기만 바꾸고 나머지 문장은 건드리지 않는다."""
    return _HOUR_WORD_RE.sub(lambda m: f"{_HOUR_WORDS[m.group(1)]}시", t)


def parse_time(text: str) -> dict | None:
    """발화에서 방문 시각을 찾는다. 날짜와 같은 규칙으로 마지막 표현을 채택한다.

    반환: {"time":"HH:MM"|None, "label", "confident", "corrected"} 또는 None.

    오전·오후를 말하지 않은 한 자리 시각은 **추측하지 않는다**. "3시"는
    오전 3시일 수도 오후 3시일 수도 있는데, 병원 시간이라는 이유로 오후로
    단정하면 그건 우리가 지어낸 정보다. time=None + confident=False 로 두고
    접수카드가 "오전인가요 오후인가요"를 묻게 한다.
    """
    t = _digitize_hours(text.replace(" ", ""))
    cands: list[tuple[int, str | None, str]] = []
    ends: list[int] = []

    for m in re.finditer(r"(오전|오후|아침|저녁|밤|낮|새벽)?(\d{1,2})시(?:(\d{1,2})분|(반))?", t):
        meridiem, hour = m.group(1), int(m.group(2))
        minute = 30 if m.group(4) else int(m.group(3) or 0)
        if hour > 23 or minute > 59:
            continue

        if meridiem in _PM_WORDS:
            if hour < 12:
                hour += 12
        elif meridiem in _AM_WORDS:
            if hour == 12:
                hour = 0
        pending_ambiguous = False
        if meridiem is None and hour <= 8:
            # 오전·오후 단서가 없고 1~8시 — 어느 쪽인지 알 수 없다
            pending_ambiguous = True
        label = ((meridiem + " ") if meridiem else "") + f"{m.group(2)}시"
        if m.group(4):
            label += " 반"
        elif m.group(3):
            label += f" {m.group(3)}분"

        if pending_ambiguous:
            cands.append((m.start(), None, label))
            ends.append(m.end())
            continue

        cands.append((m.start(), f"{hour:02d}:{minute:02d}", label))
        ends.append(m.end())

    if not cands:
        return None

    value, label, corrected, confident, why = _resolve(cands, t, ends)
    # 오전·오후를 몰라 value 가 None 인 경우도 확정할 수 없다
    if confident and value is None:
        why = AMBIGUOUS_MERIDIEM
    return {"time": value, "label": label,
            "confident": confident and value is not None,
            "corrected": corrected, "ambiguous": why}


def spoken_datetime(date: str | None, time: str | None) -> str:
    """ISO(2026-08-20 / 14:30) → 한국어 표기(2026년 8월 20일 14시 30분).

    parse_date·parse_time 의 역방향이다. 여기 두는 이유는 **이 파일의 파서가
    읽을 수 있는 표기만 내보내야** 하기 때문이다 — 둘이 떨어져 있으면 한쪽만
    고쳐서 어긋난다.

    보호자 포털의 구조화 신청을 기존 파이프라인에 태울 때 쓴다. ISO 를 그대로
    문장에 넣었더니 parse_date 도 parse_time 도 못 읽어서, 보호자가 달력에서
    고른 날짜가 통째로 버려졌다. date 가 None 이 되면 방문일이 '확인 필요' 로
    남는데 그건 gate.BLOCKING 항목이라 **확정이 409 로 막힌다** — 보호자가
    정확히 적어 보낸 일정을 사회복지사가 전화로 다시 확인해야 했다.

    연도를 붙이는 것은 연말 경계 때문이다. '1월 5일' 만으로도 파서가 내년으로
    잡아주지만, 우리가 이미 아는 값을 추론에 맡길 이유가 없다.
    """
    out = ""
    if date:
        y, m, d = date.split("-")
        out = f"{int(y)}년 {int(m)}월 {int(d)}일"
    if time:
        hh, mm = time.split(":")
        # '14시 0분' 은 어색하다. 정각이면 분을 뺀다.
        out += (" " if out else "") + f"{int(hh)}시" + (f" {int(mm)}분" if int(mm) else "")
    return out
