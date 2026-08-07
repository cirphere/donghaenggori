"""지역명 → 대표 좌표.

케어 프로필에는 읍면동 텍스트만 있고 좌표가 없다. 신규(cold start) 대상자에게
'거리 기준 참고 후보'를 제시하려면 기준점이 필요하므로, 시군구 청사 좌표를 쓴다.

한계를 분명히 해둔다.
  · 시군구 대표 좌표라 실제 거주지와 수 km 차이가 날 수 있다.
  · 그래서 결과는 '확정 후보'가 아니라 **참고값**으로만 표시한다.
  · 실운영에서는 케어 프로필에 좌표를 저장해 이 근사를 대체한다.
"""
from __future__ import annotations

# 시군구 → (위도, 경도). 시연 데이터에 등장하는 지역만 수록.
REGION_COORDS: dict[str, tuple[float, float]] = {
    # 광주광역시
    "광주광역시 동구": (35.1460, 126.9231),
    "광주광역시 서구": (35.1520, 126.8895),
    "광주광역시 남구": (35.1330, 126.9024),
    "광주광역시 북구": (35.1740, 126.9120),
    "광주광역시 광산구": (35.1396, 126.7937),
    # 전라남도
    "전남 고흥군": (34.6111, 127.2850),
    "전남 신안군": (34.8334, 126.3512),
    "전남 보성군": (34.7714, 127.0800),
    "전남 담양군": (35.3212, 126.9882),
    "전남 곡성군": (35.2820, 127.2921),
    "전남 화순군": (35.0645, 126.9866),
    "전남 영광군": (35.2772, 126.5120),
    "전남 강진군": (34.6420, 126.7672),
    "전남 장성군": (35.3018, 126.7847),
    "전남 함평군": (35.0658, 126.5169),
}

# 시도 대표 좌표 — 시군구를 못 찾을 때의 폴백
SIDO_COORDS: dict[str, tuple[float, float]] = {
    "광주": (35.1601, 126.8514),
    "전남": (34.8161, 126.4630),
}


def coords_of(region: str | None) -> tuple[float, float] | None:
    """'광주광역시 서구 ○○동' → 서구 좌표. 못 찾으면 시도 폴백, 그래도 없으면 None."""
    if not region:
        return None
    for key, latlon in REGION_COORDS.items():
        if region.startswith(key):
            return latlon
    for key, latlon in SIDO_COORDS.items():
        if region.startswith(key):
            return latlon
    return None


def is_precise(region: str | None) -> bool:
    """시군구 단위로 매칭됐는지(True) 시도 폴백인지(False)."""
    return bool(region) and any(region.startswith(k) for k in REGION_COORDS)
