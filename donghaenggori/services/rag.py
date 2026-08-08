"""지역 복지자원 보강 (RAG) — md 파이프라인 ⑦.

접수카드에 "이 어르신 근처에서 연계할 수 있는 복지자원"을 덧붙인다.
근거 데이터는 공공데이터로 적재한 facilities 테이블(C-DS03 복지관 등)이며,
결과에는 반드시 출처를 함께 반환한다. AI가 지어낸 정보가 아님을 보이기 위해서다.

검색 방식 — 하이브리드
  · 지역 일치: 구조적 조건. 같은 구/군의 복지관이 의미적으로 아무리 비슷해도
    다른 지역 시설보다 우선한다. 어르신이 실제로 갈 수 있어야 하기 때문이다.
  · 의미 유사도: 문장 임베딩(ko-sroberta) 코사인 유사도. "무릎이 아파서"와
    "노인복지관"처럼 표현이 겹치지 않아도 연결된다.

임베딩 모델을 못 쓰는 환경(미설치·다운로드 실패)에서는 토큰 겹침 방식으로
자동 폴백한다. 발표 중 모델 문제로 접수가 막히지 않게 하기 위해서다.
"""
from __future__ import annotations

import os
import re
import threading

from ..core import db

MODEL_NAME = os.environ.get("RAG_MODEL", "jhgan/ko-sroberta-multitask")
REGION_WEIGHT = 3.0        # 같은 구/군 일치 가중
SIDO_WEIGHT = 1.0          # 같은 시도 일치 가중
SEMANTIC_WEIGHT = 2.0      # 의미 유사도 가중 (코사인 0~1에 곱함)

_TOKEN = re.compile(r"[가-힣A-Za-z0-9]+")
_lock = threading.Lock()
_state: dict = {"model": None, "emb": None, "rows": None, "tried": False}


# ------------------------------------------------------------- 임베딩 --

def _load_model():
    """모델을 한 번만 로드한다. 실패하면 None → 토큰 겹침으로 폴백."""
    if _state["tried"]:
        return _state["model"]
    with _lock:
        if _state["tried"]:
            return _state["model"]
        _state["tried"] = True
        try:
            from sentence_transformers import SentenceTransformer
            _state["model"] = SentenceTransformer(MODEL_NAME)
        except Exception:
            _state["model"] = None
        return _state["model"]


def _doc_text(r: dict) -> str:
    """시설 한 건을 검색 대상 문장으로 만든다."""
    return " ".join(x for x in (r.get("name"), r.get("kind"),
                                r.get("region"), r.get("address")) if x)


def _index(rows: list[dict]):
    """시설 목록을 임베딩해 캐시한다. 시설 수가 바뀌면 다시 만든다."""
    model = _load_model()
    if model is None:
        return None
    cached = _state.get("rows")
    if cached is not None and len(cached) == len(rows) and _state["emb"] is not None:
        return _state["emb"]
    emb = model.encode([_doc_text(r) for r in rows],
                       convert_to_numpy=True, normalize_embeddings=True,
                       show_progress_bar=False)
    _state["rows"], _state["emb"] = rows, emb
    return emb


def available() -> bool:
    return _load_model() is not None


# ------------------------------------------------------------ 검색 --

def _tokens(s: str | None) -> set[str]:
    return set(_TOKEN.findall(s or ""))


def _region_keys(region: str | None) -> list[str]:
    return [t for t in _TOKEN.findall(region or "") if len(t) >= 2]


def _region_score(region: str | None, row: dict) -> float:
    """지역 일치 점수. 구/군 단위 일치가 시도 일치보다 훨씬 크다."""
    rtok = _tokens(row.get("region"))
    score = 0.0
    for k in _region_keys(region):
        if k in rtok:
            # '광주광역시'는 시도, '서구'는 구 — 짧은 쪽이 보통 구/군이다
            score += REGION_WEIGHT if len(k) <= 3 else SIDO_WEIGHT
    return score


def search(region: str | None = None, query: str | None = None,
           limit: int = 3) -> list[dict]:
    """지역·질의로 복지자원을 찾는다. 결과에 점수 근거와 출처를 함께 담는다."""
    rows = db.search_facilities(limit=500)
    if not rows:
        return []

    emb = _index(rows) if query else None
    sims = None
    if emb is not None and query:
        model = _load_model()
        q = model.encode([query], convert_to_numpy=True,
                         normalize_embeddings=True, show_progress_bar=False)
        sims = (emb @ q[0])          # 정규화되어 있으므로 내적 = 코사인

    scored = []
    for i, r in enumerate(rows):
        rs = _region_score(region, r)
        if sims is not None:
            sem = float(sims[i])
            total = rs + SEMANTIC_WEIGHT * sem
            method = "지역+의미"
        else:
            # 폴백: 토큰 겹침
            overlap = _tokens(query) & (_tokens(r.get("name")) | _tokens(r.get("kind")))
            sem = 0.0
            total = rs + 1.5 * len(overlap)
            method = "지역+토큰겹침"
        if total <= 0:
            continue
        scored.append((total, sem, rs, method, r))

    scored.sort(key=lambda x: (-x[0], x[4]["name"]))
    out = []
    for total, sem, rs, method, r in scored[:limit]:
        out.append({
            "name": r["name"], "kind": r["kind"], "region": r["region"],
            "address": r["address"], "phone": r["phone"],
            "source": r["source"],                  # 근거 데이터 출처
            "score": round(total, 2),
            "region_score": round(rs, 2),
            "semantic": round(sem, 3),
            "method": method,
        })
    return out


def enrich(profile: dict | None, analysis) -> list[dict]:
    """파이프라인 ⑦ — 케어 프로필 지역 기준으로 인근 복지자원을 붙인다.

    질의는 원문 발화를 쓴다. 진료과만 넣으면 '정형외과'와 '복지관'이
    의미적으로 멀어 검색이 흐려진다.
    """
    if not profile:
        return []
    q = getattr(analysis, "raw", None) or getattr(analysis, "dept", None)
    return search(region=profile.get("region"), query=q, limit=3)
