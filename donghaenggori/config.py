"""환경설정 — .env 파일에서 키를 읽는다.

키는 코드·문서·대화 어디에도 적지 않는다. 프로젝트 루트의 .env 파일에만 둔다.
.env는 .gitignore에 있어 커밋되지 않는다.

사용:
    from donghaenggori.config import settings
    if settings.has_anthropic: ...
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def _load_env() -> None:
    """python-dotenv가 있으면 사용하고, 없으면 직접 파싱한다(의존성 0 폴백)."""
    if not ENV_PATH.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH, override=False)
        return
    except ImportError:
        pass
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY") or None
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL") or "claude-opus-5"
    # 요약은 짧은 메모 구조화라 오래 걸릴 이유가 없다 — 시연 중 멈추지 않게 짧게
    anthropic_timeout: float = _float("ANTHROPIC_TIMEOUT", 20.0)
    # data.go.kr은 계정당 공통 인증키 — 심평원·기상·대기오염에 같은 키를 쓴다
    data_go_kr_key: str | None = os.environ.get("DATA_GO_KR_KEY") or None
    # 외부 API — 접수 흐름을 막지 않도록 짧게 잡는다
    public_api_timeout: float = _float("PUBLIC_API_TIMEOUT", 3.0)
    public_api_cache_ttl: int = _int("PUBLIC_API_CACHE_TTL", 600)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_public_data(self) -> bool:
        return bool(self.data_go_kr_key)

    def status(self) -> dict[str, str]:
        """키 존재 여부만 보고한다 — 값은 절대 노출하지 않는다."""
        return {
            "ANTHROPIC_API_KEY": "설정됨" if self.has_anthropic else "없음 (규칙 기반으로 동작)",
            "DATA_GO_KR_KEY": "설정됨" if self.has_public_data else "없음 (심평원·기상·대기 미연동)",
        }


settings = Settings()
