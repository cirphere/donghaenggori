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

# 설정 파일은 컨테이너별로 나뉘어 있다. 도커로 띄우면 compose 의 env_file 이
# 알맞은 파일만 넣어 주지만, 도커 없이 직접 실행할 때는 여기서 읽어야 한다.
#
# .env 를 먼저 읽는 것은 예전 통합 파일 호환용이다. setdefault 라 **먼저 읽은
# 쪽이 이긴다** — 옮기는 중에 양쪽에 같은 키가 있으면 옛 값이 남는다. 옮긴 뒤에는
# 루트 .env 에서 지우는 것이 맞고, 기동 로그의 "전화 설정 —" 줄로 확인한다.
ENV_PATHS = [ROOT / ".env", ROOT / ".env.app"]
ENV_PATH = ENV_PATHS[0]          # 예전 이름 — 이 값을 참조하는 코드가 있다


def _load_env() -> None:
    """python-dotenv가 있으면 사용하고, 없으면 직접 파싱한다(의존성 0 폴백)."""
    for path in ENV_PATHS:
        if not path.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(path, override=False)
            continue
        except ImportError:
            pass
        for line in path.read_text(encoding="utf-8").splitlines():
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
    # 짧은 메모를 고정 스키마로 옮기는 작업이고 승인 게이트가 뒤에 있다.
    # 최상위 모델을 쓸 이유가 없어 Sonnet을 기본으로 둔다.
    # (Haiku 4.5로 더 내리려면 코드 수정 필요 — effort·adaptive thinking 미지원)
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5"
    # 요약은 짧은 메모 구조화라 오래 걸릴 이유가 없다 — 시연 중 멈추지 않게 짧게
    anthropic_timeout: float = _float("ANTHROPIC_TIMEOUT", 20.0)
    # data.go.kr은 계정당 공통 인증키 — 심평원·기상·대기오염에 같은 키를 쓴다
    data_go_kr_key: str | None = os.environ.get("DATA_GO_KR_KEY") or None
    # 외부 API — 접수 흐름을 막지 않도록 짧게 잡는다
    public_api_timeout: float = _float("PUBLIC_API_TIMEOUT", 3.0)
    public_api_cache_ttl: int = _int("PUBLIC_API_CACHE_TTL", 600)
    # 로그인 세션 유효기간(초). 기본 12시간 — 내부 소수 인원용 도구라 짧게
    # 만료돼도 다시 로그인하는 부담이 크지 않다.
    session_ttl_seconds: int = _int("SESSION_TTL_SECONDS", 43200)

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
