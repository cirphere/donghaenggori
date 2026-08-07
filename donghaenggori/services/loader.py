"""공공데이터 CSV → facilities 테이블 적재.

패키지 내 data/raw 의 원본 CSV를 읽어 지역 복지자원 검색(RAG)의 근거 데이터로 만든다.
주의: 제공 CSV 중 일부는 '개별 시설 레코드'가 아니라 '자치구별 집계표'다.
      집계표는 시설 검색에 쓸 수 없으므로 별도 표기하고 적재 대상에서 제외한다.
"""
from __future__ import annotations

import csv
import os

from ..core import db

# 원본 CSV는 패키지 안에 함께 배포한다 — clone 직후 바로 적재되도록.
_RAW = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "raw")

# (파일명, 식별ID, 유형) — 유형 point=개별 시설, aggregate=집계표
SOURCES = [
    ("광주광역시_종합사회복지관 현황_11_04_2020.csv", "C-DS03", "point"),
    ("광주광역시_사회복지 이용시설 현황_20251231.csv", "C-DS06", "aggregate"),
    ("광주광역시_시내버스 정류소 현황_20241231.csv", "C-DS14", "aggregate"),
    ("광주광역시_대기오염측정망 운영_20260531.csv", "C-DS18", "station"),
]


def _read(path: str) -> list[dict]:
    """공공데이터 CSV는 인코딩이 제각각이라 순차 시도한다."""
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            with open(path, encoding=enc) as f:
                return [{(k or "").strip(): (v or "").strip()
                         for k, v in row.items()} for row in csv.DictReader(f)]
        except (UnicodeDecodeError, LookupError):
            continue
    raise RuntimeError(f"인코딩 판별 실패: {path}")


def load_welfare_centers(path: str) -> list[dict]:
    """C-DS03 광주 종합사회복지관 — 개별 시설 레코드."""
    out = []
    for r in _read(path):
        name = r.get("복지관명")
        if not name:
            continue
        out.append({
            "source": "C-DS03", "name": name, "kind": "종합사회복지관",
            "region": f"광주광역시 {r.get('구별','')}".strip(),
            "address": r.get("소재지도로명주소"), "phone": r.get("전화번호"),
            "lat": None, "lon": None,
        })
    return out


def load_air_stations(path: str) -> list[dict]:
    """C-DS18 대기오염측정망 — 측정소 목록(외출 위험 보조 판단의 기준점)."""
    out = []
    for r in _read(path):
        name = r.get("측정소")
        if not name:
            continue
        pm10 = r.get("미세먼지(PM-10)") or ""
        out.append({
            "source": "C-DS18", "name": f"{name} 대기측정소", "kind": "대기측정소",
            "region": "광주광역시", "address": None,
            "phone": f"PM10={pm10}" if pm10 else None,
            "lat": None, "lon": None,
        })
    return out


LOADERS = {"C-DS03": load_welfare_centers, "C-DS18": load_air_stations}


def run(verbose: bool = True) -> dict:
    """적재 실행. 반환: {식별ID: 건수 또는 사유}"""
    db.init_db()
    conn = db.get_conn()
    conn.execute("DELETE FROM facilities")
    conn.commit()
    conn.close()

    report: dict[str, object] = {}
    rows_all: list[dict] = []
    for fname, sid, kind in SOURCES:
        path = os.path.join(_RAW, fname)
        if not os.path.exists(path):
            report[sid] = "파일 없음"
            continue
        if kind == "aggregate":
            report[sid] = "집계표 — 개별 시설 레코드 아님(적재 제외)"
            continue
        rows = LOADERS[sid](path)
        rows_all += rows
        report[sid] = len(rows)

    if rows_all:
        db.bulk_insert_facilities(rows_all)
    if verbose:
        for k, v in report.items():
            print(f"  {k}: {v}")
    return report


if __name__ == "__main__":
    run()
