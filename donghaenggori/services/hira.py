"""심평원(HIRA) 병원·의원·약국 정보 — 병원 후보 보강.

md 5-2: "병원·약국 정보는 별첨에 없어 외부 공공데이터로 보강"

다만 병원 후보의 1차 근거는 어디까지나 과거 동행 이력이다. 심평원은 보강용이며
  · 이력이 있는 병원 → 주소·전화·종별·진료과 등 부가정보를 붙인다
  · 이력이 없는 신규(cold start) → 거리 기준 '참고 후보'만 제시하고 확정하지 않는다
    (화면 04 4-A "이력 근거 없음 — 거리 기준 참고값")

API: 건강보험심사평가원 병원정보서비스 (data.go.kr)
"""
from __future__ import annotations

from . import _client

BASE = "http://apis.data.go.kr/B551182/hospInfoServicev2"
LIST_URL = f"{BASE}/getHospBasisList"

# 진료과 → 심평원 진료과목코드
DGSBJT = {
    "내과": "01", "신경과": "02", "정신건강의학과": "03", "외과": "04",
    "정형외과": "05", "신경외과": "06", "산부인과": "10", "소아청소년과": "11",
    "안과": "12", "이비인후과": "13", "피부과": "14", "비뇨의학과": "15",
    "영상의학과": "17", "재활의학과": "21", "가정의학과": "23", "치과": "49",
}


def enabled() -> bool:
    return _client.enabled()


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize(item: dict) -> dict:
    """심평원 응답을 접수카드가 쓰는 형태로 변환한다."""
    return {
        "name": item.get("yadmNm"),
        "kind": item.get("clCdNm"),                 # 종합병원 / 의원 등
        "address": item.get("addr"),
        "phone": item.get("telno"),
        "sido": item.get("sidoCdNm"),
        "sggu": item.get("sgguCdNm"),
        "lat": _f(item.get("YPos")), "lon": _f(item.get("XPos")),
        "distance_m": _f(item.get("distance")),
        "source": "심평원 병원정보서비스",
    }


def search_hospitals(name: str | None = None, dept: str | None = None,
                     sido_cd: str | None = None, rows: int = 5) -> _client.ApiResult:
    """병원명·진료과로 조회. 키가 없으면 미연동 상태를 그대로 반환한다."""
    params: dict = {"pageNo": 1, "numOfRows": rows}
    if name:
        params["yadmNm"] = name
    if dept and dept in DGSBJT:
        params["dgsbjtCd"] = DGSBJT[dept]
    if sido_cd:
        params["sidoCd"] = sido_cd

    res = _client.get_json(LIST_URL, params)
    if res.unavailable:
        return res
    return _client.ApiResult(True, [_normalize(i) for i in _client.items_of(res.data)],
                             cached=res.cached)


def nearby(lat: float, lon: float, dept: str | None = None,
           radius_m: int = 5000, rows: int = 5) -> _client.ApiResult:
    """좌표 반경 검색 — cold start 시 '참고 후보'를 만드는 데 쓴다.

    반환 항목에는 근거가 거리뿐임을 명시한다. 확정 후보로 쓰면 안 된다.
    """
    params: dict = {"pageNo": 1, "numOfRows": rows,
                    "xPos": lon, "yPos": lat, "radius": radius_m}
    if dept and dept in DGSBJT:
        params["dgsbjtCd"] = DGSBJT[dept]

    res = _client.get_json(LIST_URL, params)
    if res.unavailable:
        return res
    out = []
    for i in _client.items_of(res.data):
        n = _normalize(i)
        n["basis"] = "이력 근거 없음 — 거리 기준 참고값"
        out.append(n)
    return _client.ApiResult(True, out, cached=res.cached)


def enrich_candidate(hospital_name: str, dept: str | None = None) -> dict | None:
    """이력에 있는 병원명에 심평원 부가정보를 붙인다. 실패하면 None(폴백)."""
    res = search_hospitals(name=hospital_name, dept=dept, rows=1)
    if res.unavailable or not res.data:
        return None
    return res.data[0]
