"""병원 후보 생성 — 과거 동행 이력 기반 (계획서 기능 3 / 데이터 설계).

핵심: 병원 후보의 1차 근거는 외부 병원 DB가 아니라 어르신의 과거 동행 이력.
상태는 % 확신도가 아니라 누구나 검증 가능한 결정적 규칙으로 3단계를 부여한다.
  - 확인됨 : 발화에 직접 명시 또는 최근 6개월 같은 병원/진료과 2회 이상
  - 추정   : 이력·문맥으로 합리적 1순위(2순위와 분리)
  - 확인 필요 : 후보 비등 / 발화-이력 모순 / 이력 없음(신규)
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from .korean import particle

_WINDOW_DAYS = 180  # 최근 6개월


@dataclass
class HospitalResult:
    status: str                       # 확인됨 | 추정 | 확인 필요 | (긴급/약국 등은 별도)
    hospital: str | None = None       # 1순위 병원명
    dept: str | None = None
    reasons: list[str] = field(default_factory=list)   # 근거(설명가능성)
    candidates: list[dict] = field(default_factory=list)  # 후보 목록(병원, 횟수)
    need_confirm: bool = True         # 사회복지사 확인 필요 여부


def _recent_history(history: list[dict], today: datetime.date) -> list[dict]:
    out = []
    for h in history:
        try:
            d = datetime.date.fromisoformat(h["date"])
        except (KeyError, ValueError):
            continue
        if (today - d).days <= _WINDOW_DAYS:
            out.append(h)
    return out


def suggest(profile: dict | None, dept: str | None, today: datetime.date | None = None,
            spoken: str | None = None) -> HospitalResult:
    """병원 후보를 정한다. 발화에 이름이 나왔으면 그것이 최우선이다.

    독스트링에는 처음부터 "확인됨 : 발화에 직접 명시 또는 …" 이라고 적어 뒀는데
    직접 명시 쪽이 구현돼 있지 않았다. 실통화에서 "내일 송정병원으로 가야 될 것
    같아" 를 받고도 이력의 다른 병원을 '확인됨' 으로 내놓았다 — 어르신이 댄
    이름을 무시하고 엉뚱한 곳으로 배차될 뻔했다.
    """
    if today is None:
        today = datetime.date.today()

    if spoken:
        reasons = [f"원문에서 '{spoken}'{particle(spoken, '을')} 직접 언급"]
        # 이력과 다르면 그 사실도 남긴다. 바꾸지는 않는다 — 어르신이 말한 것이
        # 우선이고, 다른 곳으로 옮겼을 수도 있다. 판단은 사회복지사가 한다.
        recent = _recent_history(profile.get("history") or [], today) if profile else []
        others = {h.get("hospital") for h in recent if h.get("hospital")} - {spoken}
        if others:
            reasons.append("과거 이력과 다름 — 최근 방문: " + ", ".join(sorted(others)))
        return HospitalResult(status="확인됨", hospital=spoken, dept=dept,
                              reasons=reasons, need_confirm=bool(others))

    # 신규(cold start): 이력 없음 → 확인 필요. 증상·위치 기반 추천은 본선 확장.
    if not profile or not profile.get("history"):
        return HospitalResult(
            status="확인 필요",
            dept=dept,
            reasons=["과거 동행 이력이 없는 신규 대상자 — 병원명을 사회복지사가 확인 필요"],
            need_confirm=True,
        )

    recent = _recent_history(profile["history"], today)
    pool = recent or profile["history"]  # 6개월 내 없으면 전체 이력으로 폴백

    # 진료과로 후보 압축(진료과를 알 때)
    if dept:
        matched = [h for h in pool if h.get("dept") == dept]
    else:
        matched = list(pool)

    if not matched:
        # 발화 진료과와 이력이 모순/불일치 → 확인 필요(이력 우선시 금지)
        return HospitalResult(
            status="확인 필요",
            dept=dept,
            reasons=[f"발화의 진료과('{dept}')와 일치하는 과거 이력이 없음 — 새 증상일 수 있어 확인 필요"],
            candidates=_count_hospitals(pool),
            need_confirm=True,
        )

    counts = _count_hospitals(matched)
    top = counts[0]

    # 확인됨: 같은 병원 2회 이상
    if top["count"] >= 2:
        return HospitalResult(
            status="확인됨",
            hospital=top["hospital"],
            dept=dept or top["dept"],
            reasons=[f"최근 6개월 내 {top['hospital']}({top['dept']}) {top['count']}회 방문 — 단골로 확인됨"],
            candidates=counts,
            need_confirm=True,  # 확인됨이어도 최종 확정 통화는 사람이
        )

    # 후보가 여럿이고 1·2순위가 동률 → 확인 필요
    if len(counts) >= 2 and counts[0]["count"] == counts[1]["count"]:
        return HospitalResult(
            status="확인 필요",
            dept=dept,
            reasons=["비슷한 후보가 둘 이상 — 어느 병원인지 확인 필요"],
            candidates=counts,
            need_confirm=True,
        )

    # 추정: 합리적 1순위
    return HospitalResult(
        status="추정",
        hospital=top["hospital"],
        dept=dept or top["dept"],
        reasons=[f"과거 이력상 {top['hospital']}({top['dept']}) 1회 방문 — 추정(확정 전 확인 권장)"],
        candidates=counts,
        need_confirm=True,
    )


def _count_hospitals(entries: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for h in entries:
        name = h.get("hospital")
        if not name:
            continue
        if name not in agg:
            agg[name] = {"hospital": name, "dept": h.get("dept"), "count": 0, "last": h.get("date")}
        agg[name]["count"] += 1
        if h.get("date", "") > (agg[name]["last"] or ""):
            agg[name]["last"] = h.get("date")
    # 횟수 내림차순, 같으면 최근 방문 우선
    return sorted(agg.values(), key=lambda x: (x["count"], x["last"] or ""), reverse=True)
