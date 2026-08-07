"""지역 복지자원 보강 (RAG) — md 파이프라인 ⑦.

접수카드에 "이 어르신 근처의 복지자원" 정보를 덧붙인다.
근거 데이터는 공공데이터로 적재한 facilities 테이블(C-DS03 복지관 등).

검색 방식: BM25 계열 점수 없이도 충분한 규모(수십~수백 건)이므로
토큰 겹침 + 지역 일치 가중 방식을 쓴다. 의존성 0, CPU 즉시.
데이터가 수만 건으로 늘면 임베딩 검색으로 교체(인터페이스 동일).

주의: AI가 지어낸 정보가 아니라 공공데이터 출처를 함께 반환한다.
"""
from __future__ import annotations

import re

from ..core import db

_TOKEN = re.compile(r"[가-힣A-Za-z0-9]+")


def _tokens(s: str | None) -> set[str]:
    return set(_TOKEN.findall(s or ""))


def _region_keys(region: str | None) -> list[str]:
    """'광주광역시 서구' → ['광주광역시','서구'] / '전남 ○○군 ○○면' → [...]"""
    return [t for t in _TOKEN.findall(region or "") if len(t) >= 2]


def search(region: str | None = None, query: str | None = None,
           limit: int = 3) -> list[dict]:
    """지역·질의어로 복지자원을 찾아 점수순으로 반환한다."""
    rows = db.search_facilities(limit=500)
    if not rows:
        return []

    rkeys = _region_keys(region)
    qtok = _tokens(query)
    scored = []
    for r in rows:
        score = 0.0
        rtok = _tokens(r.get("region"))
        # 지역 일치가 가장 중요 (구·군 단위 일치에 큰 가중)
        for k in rkeys:
            if k in rtok:
                score += 3.0
        if qtok:
            overlap = qtok & (_tokens(r.get("name")) | _tokens(r.get("kind")))
            score += 1.5 * len(overlap)
        if score > 0:
            scored.append((score, r))

    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    out = []
    for score, r in scored[:limit]:
        out.append({
            "name": r["name"], "kind": r["kind"], "region": r["region"],
            "address": r["address"], "phone": r["phone"],
            "source": r["source"],                      # 근거 데이터 출처 표시
            "score": round(score, 2),
        })
    return out


def enrich(profile: dict | None, analysis) -> list[dict]:
    """파이프라인 ⑦ — 케어 프로필 지역 기준으로 인근 복지자원을 붙인다."""
    if not profile:
        return []
    return search(region=profile.get("region"),
                  query=getattr(analysis, "dept", None), limit=3)
