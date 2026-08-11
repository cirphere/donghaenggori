"""상대 시간 표현을 실제 날짜로 해석하는 규칙 기반 파서.

계획서 6장: "날짜·상대시간 표현은 규칙 기반 파서로 1차 처리하고 모호 문맥만 LLM이 보완".
어르신 발화의 '모레', '다음주 화요일' 같은 표현을 결정적 규칙으로 처리해 안정성을 확보한다.
"""
from __future__ import annotations

import datetime
import re

_WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_REL_DAYS = {"오늘": 0, "낼": 1, "내일": 1, "모레": 2, "모래": 2, "글피": 3}


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

    cands: list[tuple[int, str, str]] = []      # (위치, 날짜, 원문 표현)

    # 1) 오늘/내일/모레/글피
    for word, delta in _REL_DAYS.items():
        for m in re.finditer(re.escape(word), t):
            d = today + datetime.timedelta(days=delta)
            cands.append((m.start(), d.isoformat(), word))

    # 2) (이번주/다음주/담주) + 요일
    for m in re.finditer(r"(이번주|다음주|담주)?([월화수목금토일])요일", t):
        base_week, wd = m.group(1), _WEEKDAYS[m.group(2)]
        days_ahead = (wd - today.weekday()) % 7
        if base_week in ("다음주", "담주"):
            days_ahead += 7
        elif days_ahead == 0:  # 요일만 말했고 오늘이면 다음 주기로
            days_ahead = 7
        d = today + datetime.timedelta(days=days_ahead)
        label = (f"{base_week} " if base_week else "") + f"{m.group(2)}요일"
        cands.append((m.start(), d.isoformat(), label))

    # 3) N일 뒤/후
    for m in re.finditer(r"(\d+)일(뒤|후)", t):
        d = today + datetime.timedelta(days=int(m.group(1)))
        cands.append((m.start(), d.isoformat(), f"{m.group(1)}일 {m.group(2)}"))

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

    if not cands:
        return None

    cands.sort(key=lambda c: c[0])
    _, date_value, label = cands[-1]
    # 표현이 여러 번 나와도 가리키는 날짜가 하나면 정정이 아니다("내일 내일")
    corrected = len({c[1] for c in cands}) > 1
    return {"date": date_value, "label": label, "confident": True, "corrected": corrected}


# 오후로 읽어야 하는 말들. '낮 1시'는 13시, '낮 12시'는 12시다.
_PM_WORDS = ("오후", "저녁", "밤", "낮")
_AM_WORDS = ("오전", "아침", "새벽")


def parse_time(text: str) -> dict | None:
    """발화에서 방문 시각을 찾는다. 날짜와 같은 규칙으로 마지막 표현을 채택한다.

    반환: {"time":"HH:MM"|None, "label", "confident", "corrected"} 또는 None.

    오전·오후를 말하지 않은 한 자리 시각은 **추측하지 않는다**. "3시"는
    오전 3시일 수도 오후 3시일 수도 있는데, 병원 시간이라는 이유로 오후로
    단정하면 그건 우리가 지어낸 정보다. time=None + confident=False 로 두고
    접수카드가 "오전인가요 오후인가요"를 묻게 한다.
    """
    t = text.replace(" ", "")
    cands: list[tuple[int, str | None, str]] = []

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
            continue

        cands.append((m.start(), f"{hour:02d}:{minute:02d}", label))

    if not cands:
        return None

    cands.sort(key=lambda c: c[0])
    _, value, label = cands[-1]
    corrected = len({c[1] for c in cands}) > 1
    return {"time": value, "label": label, "confident": value is not None,
            "corrected": corrected}
