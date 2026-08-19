"""에어코리아 대기오염정보 — 외출 전 체크리스트 (화면 03 접수카드).

md 5-2: "C-DS18/19 대기오염 — 미세먼지 나쁜 날 외출 위험 보조 판단"

기상과 마찬가지로 방문 가부는 판단하지 않는다. 미세먼지 등급을 참고 정보로만 표시한다.
API: 한국환경공단 에어코리아 시도별 실시간 측정정보 (data.go.kr)
"""
from __future__ import annotations

from . import _client

URL = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"

# 시도명 — API가 요구하는 표기
SIDO = {
    "광주": "광주", "광주광역시": "광주",
    "전남": "전남", "전라남도": "전남",
    "서울": "서울", "경기": "경기", "인천": "인천", "부산": "부산", "대구": "대구",
    "대전": "대전", "울산": "울산", "세종": "세종", "강원": "강원",
    "충북": "충북", "충남": "충남", "전북": "전북", "경북": "경북", "경남": "경남", "제주": "제주",
}

# 등급 코드 (API grade: 1좋음 2보통 3나쁨 4매우나쁨)
GRADE = {"1": "좋음", "2": "보통", "3": "나쁨", "4": "매우나쁨"}
BAD_GRADES = {"3", "4"}


def enabled() -> bool:
    return _client.enabled()


def sido_of(region: str | None) -> str | None:
    """'광주광역시 서구 ○○동' → '광주' 처럼 시도명을 뽑는다."""
    if not region:
        return None
    for key, val in SIDO.items():
        if region.startswith(key):
            return val
    return None


def realtime(region: str) -> _client.ApiResult:
    """시도 단위 실시간 측정값. 측정소별 값을 모아 대표 등급을 낸다."""
    sido = sido_of(region)
    if not sido:
        return _client.ApiResult(False, reason=f"시도 판별 불가: {region}")

    res = _client.get_json(URL, {
        "returnType": "json", "numOfRows": 100, "pageNo": 1,
        "sidoName": sido, "ver": "1.0",
    })
    if res.unavailable:
        return res

    items = _client.items_of(res.data)
    pm10, pm25, bad_stations = [], [], []
    for it in items:
        g10, g25 = it.get("pm10Grade"), it.get("pm25Grade")
        try:
            if it.get("pm10Value") not in (None, "-"):
                pm10.append(int(it["pm10Value"]))
            if it.get("pm25Value") not in (None, "-"):
                pm25.append(int(it["pm25Value"]))
        except (TypeError, ValueError):
            pass
        if g10 in BAD_GRADES or g25 in BAD_GRADES:
            bad_stations.append(it.get("stationName"))

    data = {
        "sido": sido,
        "pm10_avg": round(sum(pm10) / len(pm10)) if pm10 else None,
        "pm25_avg": round(sum(pm25) / len(pm25)) if pm25 else None,
        "stations": len(items),
        "bad_stations": bad_stations[:5],
        "cautions": _cautions(pm10, pm25, bad_stations),
        "source": "에어코리아 대기오염정보",
    }
    return _client.ApiResult(True, data, cached=res.cached)


def _cautions(pm10: list[int], pm25: list[int], bad: list) -> list[str]:
    """외출 전 체크리스트 문구. 환경부 기준(PM10 81↑, PM2.5 36↑ = 나쁨)."""
    out: list[str] = []
    if pm10:
        avg = sum(pm10) / len(pm10)
        if avg >= 81:
            out.append(f"미세먼지 나쁨 (PM10 {avg:.0f}㎍/㎥) → 마스크 준비 안내")
    if pm25:
        avg = sum(pm25) / len(pm25)
        if avg >= 36:
            out.append(f"초미세먼지 나쁨 (PM2.5 {avg:.0f}㎍/㎥) → 외출 시간 단축 권장")
    if bad and not out:
        out.append(f"일부 측정소 대기질 나쁨 ({', '.join(str(b) for b in bad[:2])} 등)")

    # **문제가 없을 때도 한 줄 남긴다.**
    #
    # 예전에는 '나쁨' 일 때만 문구를 냈다. 공기가 좋은 날에는 이 칸이 통째로
    # 비는데, 화면에서는 "확인했고 문제없음" 과 "확인하지 않음" 이 똑같이
    # 보인다. 복지사에게는 그 둘이 다르다 — 동행을 내보내기 전에 봤는지
    # 여부가 걸린다.
    #
    # 판단은 여전히 하지 않는다. 수치와 등급만 적고 "나가도 된다" 는 말은
    # 넣지 않는다(문서 4-1: 방문 가부를 AI 가 결정하지 않는다).
    if not out and (pm10 or pm25):
        parts = []
        if pm10:
            parts.append(f"PM10 {sum(pm10) / len(pm10):.0f}")
        if pm25:
            parts.append(f"PM2.5 {sum(pm25) / len(pm25):.0f}")
        out.append(f"미세먼지 보통 이하 ({' · '.join(parts)}㎍/㎥) — 특이사항 없음")
    return out


def checklist(region: str) -> list[str]:
    """접수카드에 바로 넣을 문구 목록. 실패하면 빈 목록(폴백)."""
    res = realtime(region)
    return res.data["cautions"] if res.ok else []
