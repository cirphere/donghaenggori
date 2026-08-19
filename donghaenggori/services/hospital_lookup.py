"""검증된 병원 목록 조회 — **우리가 가진 데이터 안에서만** 후보를 찾는다.

신규병원탐색·진료과기반탐색에서 쓴다. 어르신이 "주변에 어떤 병원이 있나"라고
물었을 때, 모델이 아는 병원 이름을 대는 것이 이 서비스에서 제일 위험한 지어내기다
— 없는 병원으로 어르신이 헛걸음하고, 그 이름이 카드·DB·전화 안내까지 흘러간다.

그래서 이 모듈은 **출처가 있는 것만** 돌려준다.

  · 출처는 심평원 병원정보서비스(`services/hira.py`) 하나다
  · 조회가 안 되면 **후보를 만들지 않고 그 사실을 문장으로 돌려준다**
    (키 미설정 · 좌표 없음 · API 실패 · 결과 0건 — 넷을 구분한다)
  · 돌려주는 항목은 전부 `추정 후보 — 사회복지사 확인 필요` 다. '확인됨'이 되는
    경로가 없다. 조회됐다는 사실은 그 병원이 존재한다는 뜻이지, 어르신이 그곳에
    간다는 뜻이 아니다

`pipeline._reference_candidates`(이력 없는 접수의 '거리 기준 참고값')와 같은
데이터를 쓰지만 쓰임이 다르다. 저쪽은 우리가 묻지 않았는데 붙여 주는 참고값이고,
여기는 **어르신이 찾아 달라고 말한** 경우다. 근거 문장이 그래서 다르다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 조회된 후보에 항상 붙는 상태. 다른 값이 되는 경로는 없다.
STATUS = "추정 후보 — 사회복지사 확인 필요"

DEFAULT_RADIUS_M = 6000
DEFAULT_ROWS = 3


@dataclass
class LookupResult:
    """조회 결과. **available=False 여도 후보는 비어 있다** — 못 찾은 것을 만들지 않는다."""

    available: bool
    candidates: list[dict] = field(default_factory=list)
    # 사람이 그대로 읽는 문장. 카드 근거(evidence)에 실린다.
    note: str = ""
    source: str | None = None

    def to_dict(self) -> dict:
        return {"available": self.available, "candidates": self.candidates,
                "note": self.note, "source": self.source}


def lookup(dept: str | None = None, lat: float | None = None, lon: float | None = None,
           radius_m: int = DEFAULT_RADIUS_M, rows: int = DEFAULT_ROWS,
           location_label: str | None = None) -> LookupResult:
    """조건에 맞는 기관을 **가진 데이터에서만** 찾는다.

    dept 는 어르신이 **직접 말한** 진료과만 넘긴다. 증상에서 우리가 추정한
    진료과로 조회하면, 우리 추정이 조회 조건이 되어 결과가 사실처럼 굳는다.
    """
    from . import hira

    if not hira.enabled():
        # 키가 없다. 여기서 아무거나 돌려주면 그게 지어내기다.
        return LookupResult(False, note=(
            "병원 목록이 연동돼 있지 않아(DATA_GO_KR_KEY 미설정) 후보를 조회하지 "
            "못했습니다 — 사회복지사가 직접 확인해 주세요"))

    if lat is None or lon is None:
        return LookupResult(False, note=(
            "어느 지역에서 찾을지 확인되지 않아 조회하지 못했습니다"
            f"{f' (말씀하신 조건: {location_label})' if location_label else ''}"
            " — 대상자 지역을 확인한 뒤 다시 조회해야 합니다"))

    # 진료과 코드가 없는 과목은 조건에서 뺀다. 무리하게 매핑하면 엉뚱한 과목으로
    # 걸러진 목록이 '요청하신 진료과'라는 얼굴로 나간다.
    dept_note = None
    if dept and dept not in hira.DGSBJT:
        dept_note = f"'{dept}'는 조회 코드가 없어 진료과 조건 없이 찾았습니다"
        dept = None

    try:
        res = hira.nearby(lat, lon, dept=dept, radius_m=radius_m, rows=rows)
    except Exception as e:                       # 네트워크·파싱 오류 등
        return LookupResult(False, note=(
            f"병원 목록 조회에 실패했습니다({type(e).__name__}) — 후보 없음, "
            "사회복지사가 직접 확인해 주세요"))

    if res.unavailable:
        return LookupResult(False, note=(
            "병원 목록 조회에 실패했습니다 — 후보 없음, 사회복지사가 직접 확인해 주세요"))

    matched_by = f"{dept} 진료과목 보유" if dept else "진료과 조건 없음"
    out = []
    for h in res.data or []:
        # 심평원이 준 값만 옮긴다. 여기에 없는 필드를 채우지 않는다.
        out.append({
            "name": h.get("name"),
            "kind": h.get("kind"),
            "address": h.get("address"),
            "phone": h.get("phone"),
            "distance_m": h.get("distance_m"),
            "matched_by": matched_by,
            "status": STATUS,
            "source": h.get("source"),
            "basis": "요청하신 조건으로 조회한 기관 — 방문 여부는 확인되지 않음",
        })

    if not out:
        note = "조건에 맞는 기관이 조회되지 않았습니다 — 사회복지사가 직접 확인해 주세요"
        if dept_note:
            note = f"{dept_note}. {note}"
        return LookupResult(True, note=note, source="심평원 병원정보서비스")

    note = (f"심평원 병원정보서비스에서 {len(out)}곳 조회 — {STATUS}"
            f" (반경 {radius_m // 1000}km"
            f"{f', {location_label} 기준' if location_label else ''})")
    if dept_note:
        note = f"{dept_note}. {note}"
    return LookupResult(True, candidates=out, note=note, source="심평원 병원정보서비스")
