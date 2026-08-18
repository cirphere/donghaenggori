"""공공데이터포털 API 공통 클라이언트.

세 API(심평원·기상청·에어코리아)가 같은 인증키를 쓰고 같은 실패 양상을 갖는다.
  · 키가 없으면 호출하지 않고 곧바로 비활성 상태를 반환한다(예외 아님).
  · 응답 지연·장애 시 타임아웃 후 폴백한다 — 접수 흐름을 절대 막지 않는다.
    (파일1 잔여: "외부 API 응답 지연 시 타임아웃·폴백 처리")
  · 동일 요청은 TTL 캐시로 재사용한다. 시연 중 반복 호출로 쿼터를 소모하지 않게.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import settings

TIMEOUT = settings.public_api_timeout        # 초 — 접수 응답성을 위해 짧게 잡는다
CACHE_TTL = settings.public_api_cache_ttl    # 초
_cache: dict[str, tuple[float, Any]] = {}
# 캐시 키에 좌표·날짜·검색어가 들어간다. 대상자와 방문일이 늘어날수록 키가
# 계속 새로 생기는데, TTL은 읽을 때만 확인하므로 만료된 항목도 그대로 남는다.
# 오래 켜두는 서버에서 응답 JSON이 무한정 쌓이지 않게 상한을 둔다.
CACHE_MAX = 256

# 공공데이터포털 게이트웨이는 간헐적으로 요청을 구버전 엔드포인트로 302 리다이렉트한다
# (실측: 같은 키·같은 URL이 어떤 구간엔 5/5 실패, 다른 구간엔 10/10 성공).
# 리다이렉트를 따라가면 폐기된 v1이라 400이 나므로, 따라가지 않고 재시도한다.
RETRY_STATUS = {302, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 0.6                          # 초 — 시도마다 곱해서 대기


@dataclass
class ApiResult:
    ok: bool
    data: Any = None
    reason: str | None = None       # 실패·비활성 사유 (화면에 그대로 표시 가능)
    cached: bool = False

    @property
    def unavailable(self) -> bool:
        return not self.ok


def _cache_put(ck: str, data: Any) -> None:
    """TTL 캐시에 넣는다. 상한을 넘으면 만료분 → 오래된 순으로 버린다."""
    now = time.time()
    _cache[ck] = (now, data)
    if len(_cache) <= CACHE_MAX:
        return
    for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_TTL]:
        _cache.pop(k, None)
    # 전부 유효기간 안이면 넣은 순서대로 버린다 (dict는 삽입 순서를 지킨다)
    while len(_cache) > CACHE_MAX:
        _cache.pop(next(iter(_cache)))


def _key() -> str | None:
    return settings.data_go_kr_key


def enabled() -> bool:
    return bool(_key())


def get_json(url: str, params: dict, *, cache_key: str | None = None,
             timeout: float = TIMEOUT) -> ApiResult:
    """공공데이터 API 호출. 실패해도 예외를 던지지 않는다."""
    if not enabled():
        return ApiResult(False, reason="미연동 (DATA_GO_KR_KEY 없음)")

    ck = cache_key or f"{url}|{sorted(params.items())}"
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return ApiResult(True, hit[1], cached=True)

    try:
        import httpx
    except ImportError:
        return ApiResult(False, reason="httpx 미설치")

    q = dict(params)
    q["serviceKey"] = _key()
    q.setdefault("_type", "json")

    last_reason = "알 수 없는 오류"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout) as cli:
                resp = cli.get(url, params=q)   # 리다이렉트는 따라가지 않는다(폐기 엔드포인트)
        except Exception as e:
            # 타임아웃·네트워크 오류는 재시도하지 않는다.
            # 이미 timeout만큼 시간을 썼고, 재시도해도 같은 시간을 또 쓴다.
            # 재시도가 의미 있는 건 즉시 돌아오는 302/429/5xx뿐이다.
            return ApiResult(False, reason=f"{type(e).__name__} — 타임아웃/네트워크")
        else:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    # 공공데이터 API는 오류 시 XML을 반환하는 경우가 많다
                    return ApiResult(False, reason=f"JSON 아님 (응답 {resp.text[:80]})")
                _cache_put(ck, data)
                return ApiResult(True, data)

            last_reason = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRY_STATUS:
                return ApiResult(False, reason=last_reason)

        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_BACKOFF * attempt)

    return ApiResult(False, reason=f"{last_reason} ({MAX_ATTEMPTS}회 재시도 실패)")


def items_of(data: Any) -> list[dict]:
    """공공데이터 표준 응답에서 items 목록을 안전하게 꺼낸다.

    response.body.items.item 이 dict(1건) 또는 list(N건) 또는 ''(0건)으로 온다.
    """
    try:
        body = data["response"]["body"]
    except (KeyError, TypeError):
        return []
    items = (body or {}).get("items")
    if not items:
        return []
    if isinstance(items, dict):
        items = items.get("item")
    if not items:
        return []
    return items if isinstance(items, list) else [items]


def clear_cache() -> None:
    _cache.clear()
