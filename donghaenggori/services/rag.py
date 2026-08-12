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


# 시도 이름은 목록으로 판별한다. 예전에는 "짧으면 구/군"(len<=3)으로 갈랐는데,
# '광주광역시'(5) vs '서구'(2)에서는 맞지만 '전남'(2)에서 깨진다 — 전남 대상자에게
# 전남 어느 시설이든 구/군 일치 가중이 붙어, 신안군(섬) 어르신에게 100km 떨어진
# 고흥군 복지관이 '관내'로 떴다. 전남 시설을 적재하기 전에는 드러나지 않던 버그다.
_SIDO_TOKENS = {
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시", "세종특별자치시", "경기도", "강원특별자치도",
    "충청북도", "충청남도", "전북특별자치도", "전라북도", "전라남도",
    "경상북도", "경상남도", "제주특별자치도",
}


def _is_sido(token: str) -> bool:
    return token in _SIDO_TOKENS


def _region_score(region: str | None, row: dict) -> float:
    """지역 일치 점수. 구/군 단위 일치가 시도 일치보다 훨씬 크다."""
    rtok = _tokens(row.get("region"))
    score = 0.0
    for k in _region_keys(region):
        if k in rtok:
            score += SIDO_WEIGHT if _is_sido(k) else REGION_WEIGHT
    return score


# 지역 일치 정도를 숫자가 아니라 말로 남긴다. 점수만 내려주면 화면이 0.0 을
# 어떻게 읽어야 할지 모른다 — 실제로 신안군(섬) 대상자에게 100km 떨어진
# 고흥군 복지관이 아무 표시 없이 1순위로 떴다.
MATCH_LOCAL = "관내"
MATCH_SIDO = "같은 시도"
MATCH_OTHER = "타 지역"


def _region_match(region: str | None, row: dict) -> str:
    if not region:
        return MATCH_OTHER
    rtok = _tokens(row.get("region"))
    keys = _region_keys(region)
    if any(k in rtok for k in keys if not _is_sido(k)):
        return MATCH_LOCAL
    if any(k in rtok for k in keys):
        return MATCH_SIDO
    return MATCH_OTHER


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
        match = _region_match(region, r)
        out.append({
            "name": r["name"], "kind": r["kind"], "region": r["region"],
            "address": r["address"], "phone": r["phone"],
            "source": r["source"],                  # 근거 데이터 출처
            "score": round(total, 2),
            "region_score": round(rs, 2),
            "semantic": round(sem, 3),
            "method": method,
            "region_match": match,
            # 화면에 그대로 띄울 수 있는 한 줄. 관내가 아니면 반드시 밝힌다.
            "basis": (f"{r['region']} 소재 · 대상자 거주지 관내" if match == MATCH_LOCAL
                      else f"{r['region']} 소재 · 거주 시군에는 해당 시설이 없어 같은 시도에서 찾음"
                      if match == MATCH_SIDO
                      else f"{r['region']} 소재 · 대상자 거주지와 다른 지역"),
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
