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

    반환: {"date": "YYYY-MM-DD", "label": "원문 표현", "confident": bool} 또는 None.
    confident=False면 날짜를 못 찾았거나 애매한 경우 → 접수카드에서 '확인 필요'.
    """
    if today is None:
        today = datetime.date.today()
    t = text.replace(" ", "")

    # 1) 오늘/내일/모레/글피
    for word, delta in _REL_DAYS.items():
        if word in t:
            d = today + datetime.timedelta(days=delta)
            return {"date": d.isoformat(), "label": word, "confident": True}

    # 2) (이번주/다음주/담주) + 요일
    m = re.search(r"(이번주|다음주|담주)?\s*([월화수목금토일])요일", text)
    if m:
        base_week, wd = m.group(1), _WEEKDAYS[m.group(2)]
        days_ahead = (wd - today.weekday()) % 7
        if base_week in ("다음주", "담주"):
            days_ahead += 7
        elif days_ahead == 0:  # 요일만 말했고 오늘이면 다음 주기로
            days_ahead = 7
        d = today + datetime.timedelta(days=days_ahead)
        label = (f"{base_week} " if base_week else "") + f"{m.group(2)}요일"
        return {"date": d.isoformat(), "label": label, "confident": True}

    # 3) N일 뒤/후
    m = re.search(r"(\d+)\s*일\s*(뒤|후)", t)
    if m:
        d = today + datetime.timedelta(days=int(m.group(1)))
        return {"date": d.isoformat(), "label": m.group(0), "confident": True}

    # 4) M월 D일 (연도 없으면 올해, 이미 지났으면 내년)
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = datetime.date(today.year, month, day)
            if d < today:
                d = datetime.date(today.year + 1, month, day)
            return {"date": d.isoformat(), "label": f"{month}월 {day}일", "confident": True}
        except ValueError:
            pass

    return None
