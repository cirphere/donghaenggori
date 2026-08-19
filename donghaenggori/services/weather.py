"""기상청 단기예보 — 외출 전 체크리스트 (화면 03 접수카드).

md 5-2: "C-DS10 기상 정보 — 폭염 등 외출 취약일에 동행 일정·주의사항 보조"

원칙: **방문 가부를 AI가 결정하지 않는다.** 폭염·한파·강수 가능성을 참고 정보로만
표시하고, 일정 조정은 사회복지사가 판단한다.

기상청 API는 위경도가 아니라 격자좌표(nx, ny)를 쓴다. LCC 투영 변환이 필요하다.
API: 기상청 단기예보 조회서비스 (data.go.kr)
"""
from __future__ import annotations

import datetime
import math

from . import _client

URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"

# 단기예보 발표 시각 (하루 8회, 발표 후 약 10분 뒤부터 조회 가능)
_BASE_TIMES = [2, 5, 8, 11, 14, 17, 20, 23]

# 판단 기준 — 노인 외출 취약 조건
HEAT_WARNING = 33.0     # 폭염주의보 기준 일 최고기온(℃)
COLD_WARNING = -12.0    # 한파주의보 기준 일 최저기온(℃)
RAIN_POP = 60           # 강수확률(%) 이상이면 우천 대비 안내


def enabled() -> bool:
    return _client.enabled()


# ------------------------------------------------------- 격자 좌표 변환 --

def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도 → 기상청 격자좌표(nx, ny). 기상청 배포 LCC 파라미터 기준."""
    RE, GRID = 6371.00877, 5.0
    SLAT1, SLAT2, OLON, OLAT = 30.0, 60.0, 126.0, 38.0
    XO, YO = 43, 136

    DEGRAD = math.pi / 180.0
    re = RE / GRID
    slat1, slat2 = SLAT1 * DEGRAD, SLAT2 * DEGRAD
    olon, olat = OLON * DEGRAD, OLAT * DEGRAD

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = (sf ** sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / (ro ** sn)

    ra = math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5)
    ra = re * sf / (ra ** sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    nx = int(ra * math.sin(theta) + XO + 0.5)
    ny = int(ro - ra * math.cos(theta) + YO + 0.5)
    return nx, ny


def _base_datetime(now: datetime.datetime | None = None) -> tuple[str, str]:
    """가장 최근 발표 회차를 고른다(발표 후 10분 여유)."""
    now = now or datetime.datetime.now()
    ref = now - datetime.timedelta(minutes=10)
    hours = [h for h in _BASE_TIMES if h <= ref.hour]
    if hours:
        return ref.strftime("%Y%m%d"), f"{hours[-1]:02d}00"
    prev = ref - datetime.timedelta(days=1)
    return prev.strftime("%Y%m%d"), "2300"


# ------------------------------------------------------------ 조회/판정 --

def forecast(lat: float, lon: float, target_date: str | None = None) -> _client.ApiResult:
    """지정 날짜(YYYY-MM-DD)의 외출 관련 예보 요약.

    반환 data: {"date","tmx","tmn","pop_max","cautions":[...],"grid":(nx,ny)}
    """
    nx, ny = latlon_to_grid(lat, lon)
    base_date, base_time = _base_datetime()

    res = _client.get_json(URL, {
        "pageNo": 1, "numOfRows": 1000, "dataType": "JSON",
        "base_date": base_date, "base_time": base_time, "nx": nx, "ny": ny,
    })
    if res.unavailable:
        return res

    want = (target_date or datetime.date.today().isoformat()).replace("-", "")
    tmx = tmn = None
    pops: list[int] = []
    for it in _client.items_of(res.data):
        if it.get("fcstDate") != want:
            continue
        cat, val = it.get("category"), it.get("fcstValue")
        try:
            if cat == "TMX":
                tmx = float(val)
            elif cat == "TMN":
                tmn = float(val)
            elif cat == "POP":
                pops.append(int(val))
        except (TypeError, ValueError):
            continue

    pop_max = max(pops) if pops else None
    data = {
        "date": target_date or datetime.date.today().isoformat(),
        "tmx": tmx, "tmn": tmn, "pop_max": pop_max,
        "grid": {"nx": nx, "ny": ny},
        "cautions": _cautions(tmx, tmn, pop_max),
        "source": "기상청 단기예보",
    }
    return _client.ApiResult(True, data, cached=res.cached)


def _cautions(tmx: float | None, tmn: float | None, pop: int | None) -> list[str]:
    """외출 전 체크리스트 문구. 방문 가부는 판단하지 않는다."""
    out: list[str] = []
    if tmx is not None and tmx >= HEAT_WARNING:
        out.append(f"폭염 가능성 (최고 {tmx:.0f}℃) → 오전 이른 시간 권장")
    if tmn is not None and tmn <= COLD_WARNING:
        out.append(f"한파 가능성 (최저 {tmn:.0f}℃) → 방한 준비 안내")
    if pop is not None and pop >= RAIN_POP:
        out.append(f"강수확률 {pop}% → 우산·미끄럼 주의")

    # 특이사항이 없어도 한 줄 남긴다 — 조회했다는 사실이 화면에 보여야
    # "확인했고 문제없음" 과 "확인하지 않음" 이 구분된다. 판단은 하지 않는다.
    if not out and (tmx is not None or tmn is not None):
        parts = []
        if tmn is not None and tmx is not None:
            parts.append(f"{tmn:.0f}~{tmx:.0f}℃")
        elif tmx is not None:
            parts.append(f"최고 {tmx:.0f}℃")
        else:
            parts.append(f"최저 {tmn:.0f}℃")
        if pop is not None:
            parts.append(f"강수확률 {pop}%")
        out.append(f"날씨 특이사항 없음 ({' · '.join(parts)})")
    return out


def checklist(lat: float, lon: float, target_date: str | None = None) -> list[str]:
    """접수카드에 바로 넣을 문구 목록. 실패하면 빈 목록(폴백)."""
    res = forecast(lat, lon, target_date)
    return res.data["cautions"] if res.ok else []
