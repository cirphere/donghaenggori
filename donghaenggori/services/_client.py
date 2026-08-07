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


@dataclass
class ApiResult:
    ok: bool
    data: Any = None
    reason: str | None = None       # 실패·비활성 사유 (화면에 그대로 표시 가능)
    cached: bool = False

    @property
    def unavailable(self) -> bool:
        return not self.ok


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

    try:
        with httpx.Client(timeout=timeout) as cli:
            resp = cli.get(url, params=q)
        if resp.status_code != 200:
            return ApiResult(False, reason=f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            # 공공데이터 API는 오류 시 XML을 반환하는 경우가 많다
            return ApiResult(False, reason=f"JSON 아님 (응답 {resp.text[:80]})")
    except Exception as e:
        return ApiResult(False, reason=f"{type(e).__name__} — 타임아웃/네트워크")

    _cache[ck] = (time.time(), data)
    return ApiResult(True, data)


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
