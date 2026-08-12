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
    # 전남 노인복지관 명단. 한글 문서라 csv 가 아니지만, 파생 csv 를 따로 만들면
    # 원본과 어긋날 수 있어 원본을 그대로 두고 로더가 읽는다.
    ("전라남도_노인여가시설_경로당_노인복지관_2026.hwpx", "C-DS04", "point"),
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


# 전남 시군 — 표가 이 순서로 묶여 있고, 시설명에서 시군을 못 읽으면 앞 행을 잇는다.
_JEONNAM_SIGUN = [
    "목포시", "여수시", "순천시", "나주시", "광양시", "담양군", "곡성군", "구례군",
    "고흥군", "보성군", "화순군", "장흥군", "강진군", "해남군", "영암군", "무안군",
    "함평군", "영광군", "장성군", "완도군", "진도군", "신안군",
]
_JEONNAM_AREA_CODE = "061"      # 원본 표는 국번을 생략했다


def _hwpx_texts(path: str, table_index: int) -> list[str]:
    """hwpx(zip+XML)에서 표 하나의 텍스트 조각을 순서대로 뽑는다.

    한글 문서라 csv 처럼 열이 정렬돼 있지 않다. 셀 병합(본관·분관)이 있어
    고정 폭으로 자를 수 없으므로, 조각을 순서대로 받아 연번 기준으로 나눈다.
    """
    import re
    import zipfile

    with zipfile.ZipFile(path) as z:
        xml = z.read("Contents/section0.xml").decode("utf-8")
    starts = [m.start() for m in re.finditer(r"<hp:tbl", xml)]
    if len(starts) <= table_index:
        return []
    end = starts[table_index + 1] if len(starts) > table_index + 1 else len(xml)
    seg = xml[starts[table_index]:end]
    return [t.strip() for t in re.findall(r"<hp:t>([^<]*)</hp:t>", seg) if t.strip()]


def load_jeonnam_senior_centers(path: str) -> list[dict]:
    """C-DS04 전라남도 노인복지시설 — 노인복지관 개별 명단(2026. 1. 기준).

    같은 문서의 첫 표(시군별 경로당 등록 수)는 집계표라 쓰지 않는다.
    두 번째 표가 시설 하나당 한 행인 실제 명단이다.
    """
    import re

    texts = _hwpx_texts(path, table_index=1)
    if not texts:
        return []

    is_phone = re.compile(r"^\d{2,4}-\d{4}$").match
    is_name = lambda t: any(k in t for k in ("복지관", "복지센터", "복지타운"))

    # 헤더(연번·명칭·시설 소재지·전화번호·계·N개소)를 지나 첫 연번부터 읽는다
    rows, cur = [], None
    for t in texts:
        if t.isdigit() and len(t) <= 2 and not is_phone(t):
            if cur:
                rows.append(cur)
            cur = []
            continue
        if cur is not None:
            cur.append(t)
    if cur:
        rows.append(cur)

    out, sigun = [], None
    for cells in rows:
        names = [c for c in cells if is_name(c)]
        phones = [c for c in cells if is_phone(c)]
        addrs = [c for c in cells if not is_name(c) and not is_phone(c)]
        if not names:
            continue
        name = " / ".join(names)        # 법인명과 시설명이 따로 적힌 행이 있다

        # 시군은 별도 열이 없다. 시설명에서 읽고, 없으면 앞 행을 잇는다
        # (표가 시군 순으로 묶여 있어 30개소 전부 이 규칙으로 맞는다).
        found = next((s for s in _JEONNAM_SIGUN
                      if s in name or s.rstrip("시군") in name), None)
        sigun = found or sigun
        region = f"전남 {sigun}" if sigun else "전남"

        addr = addrs[0] if addrs else None
        phone = f"{_JEONNAM_AREA_CODE}-{phones[0]}" if phones else None
        out.append({
            "source": "C-DS04", "name": name, "kind": "노인복지관",
            "region": region,
            "address": f"{region} {addr}" if addr else None,
            "phone": phone, "lat": None, "lon": None,
        })
    return out


LOADERS = {"C-DS03": load_welfare_centers, "C-DS18": load_air_stations,
           "C-DS04": load_jeonnam_senior_centers}


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
