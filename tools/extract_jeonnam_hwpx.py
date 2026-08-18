"""전라남도 노인여가시설 사전정보공개(hwpx) → CSV 추출.

원본은 한글 문서라 저장소에 넣지 않는다(.gitignore 가 *.hwpx 를 막는다 —
대회 제출 문서를 코드 저장소에서 빼기로 한 규칙이다). 대신 추출한 CSV 를
커밋하고, 어떻게 뽑았는지 재현할 수 있게 이 스크립트를 남긴다.
30행이 diff 로 검토된다는 부수 효과도 있다.

    python -m tools.extract_jeonnam_hwpx <원본.hwpx>

원본 문서에는 표가 둘이다.
  표1  시군별 경로당 등록 수 — 집계표라 시설 검색에 쓸 수 없다
  표2  노인복지관 30개소 개별 명단 — 이걸 뽑는다
"""
from __future__ import annotations

import csv
import os
import re
import sys
import zipfile

OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                   "donghaenggori", "data", "raw",
                   "전라남도_노인복지관_현황_2026.csv")

# 표가 이 순서로 묶여 있고, 시설명에서 시군을 못 읽으면 앞 행을 잇는다.
SIGUN = ["목포시", "여수시", "순천시", "나주시", "광양시", "담양군", "곡성군",
         "구례군", "고흥군", "보성군", "화순군", "장흥군", "강진군", "해남군",
         "영암군", "무안군", "함평군", "영광군", "장성군", "완도군", "진도군", "신안군"]
AREA_CODE = "061"                 # 원본 표는 국번을 생략했다

_is_phone = re.compile(r"^\d{2,4}-\d{4}$").match


def _is_name(t: str) -> bool:
    return any(k in t for k in ("복지관", "복지센터", "복지타운"))


def extract(path: str) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("Contents/section0.xml").decode("utf-8")
    starts = [m.start() for m in re.finditer(r"<hp:tbl", xml)]
    if len(starts) < 2:
        raise SystemExit("표를 두 개 찾지 못했습니다 — 원본 형식이 바뀌었는지 확인하세요")
    seg = xml[starts[1]:]
    texts = [t.strip() for t in re.findall(r"<hp:t>([^<]*)</hp:t>", seg) if t.strip()]

    # 연번(1~30)을 경계로 행을 나눈다. 본관·분관 병합 셀이 있어 고정 폭으로 못 자른다.
    rows, cur = [], None
    for t in texts:
        if t.isdigit() and len(t) <= 2 and not _is_phone(t):
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
        names = [c for c in cells if _is_name(c)]
        phones = [c for c in cells if _is_phone(c)]
        addrs = [c for c in cells if not _is_name(c) and not _is_phone(c)]
        if not names:
            continue
        name = " / ".join(names)          # 법인명과 시설명이 따로 적힌 행이 있다
        found = next((s for s in SIGUN if s in name or s.rstrip("시군") in name), None)
        sigun = found or sigun
        out.append({
            "시군": sigun or "",
            "명칭": name,
            "소재지": addrs[0] if addrs else "",
            "전화번호": f"{AREA_CODE}-{phones[0]}" if phones else "",
        })
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    rows = extract(sys.argv[1])
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["시군", "명칭", "소재지", "전화번호"])
        w.writeheader()
        w.writerows(rows)
    sigun_count = len({r["시군"] for r in rows})
    print(f"{len(rows)}개소 · {sigun_count}개 시군 → {OUT}")
    missing = [r["명칭"] for r in rows if not r["시군"]]
    if missing:
        print(f"  ※ 시군 미판별 {len(missing)}건: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
