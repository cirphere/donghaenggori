"""STT — 어르신 전화 음성 → 텍스트 (md 파이프라인 ②, 화면 02).

faster-whisper 로컬 추론을 쓴다. 외부 API 키가 필요 없고 오프라인에서 돌아가므로
발표장 네트워크가 죽어도 시연이 가능하다.

고령자 발화 대응
  · 도메인 사전(진료과·병원명)을 initial_prompt로 주입해 고유명사 인식률을 올린다.
  · 인식 확신도가 임계값 미만이면 '확인 필요'로 표시해 사람에게 넘긴다
    (파일1 잔여: "STT 확신도 임계·도메인 사전 후처리").
  · 자주 틀리는 표기는 규칙으로 후보정한다.

모델은 최초 1회 다운로드 후 캐시된다. 발표 전에 미리 받아둘 것.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# 모델 크기: tiny < base < small < medium. 한국어는 small 이상을 권장한다.
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
# 디바이스: cpu | cuda | auto
#   · 맥(Apple Silicon)은 cpu만 가능 — CTranslate2에 Metal/MPS 백엔드가 없다.
#   · GPU 서버에 올릴 땐 .env에 WHISPER_DEVICE=cuda 로 전환.
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
# 연산 타입: cpu는 int8(빠름), cuda는 float16이 유리
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE") or ("float16" if DEVICE == "cuda" else "int8")
# CPU 스레드 수 — 0이면 라이브러리 기본값
CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", "0"))
# 평균 로그확률이 이 값 미만이면 사람 확인으로 넘긴다
CONFIDENCE_THRESHOLD = float(os.environ.get("STT_CONF_THRESHOLD", "-0.9"))

# 도메인 힌트 — Whisper가 진료과·복지 용어를 더 잘 잡게 한다
DOMAIN_PROMPT = (
    "병원동행 접수 통화입니다. 정형외과, 내과, 안과, 이비인후과, 치과, 재활의학과, "
    "신경과, 피부과, 보건의료원, 약국, 생활지원사, 사회복지사, 동행 매니저."
)

# STT가 자주 틀리는 표기 후보정
_FIXUPS = [
    ("정형 외과", "정형외과"), ("이비인 후과", "이비인후과"), ("재활 의학과", "재활의학과"),
    ("보건 의료원", "보건의료원"), ("이주 뒤", "2주 뒤"), ("이 주 뒤", "2주 뒤"),
    ("삼일 뒤", "3일 뒤"), ("사회 복지사", "사회복지사"), ("생활 지원사", "생활지원사"),
    # 실측에서 관찰된 오인식 (macOS 합성음 + 고령자 억양 테스트)
    ("이 주기", "2주 뒤"), ("이주기", "2주 뒤"), ("삼 주기", "3주 뒤"),
    ("일 주기", "1주 뒤"), ("가약었어", "가야겄어"),
    # dateparse가 이미 모래=모레로 처리하지만, 접수카드 표시를 위해 정규화
    ("모래 ", "모레 "),
]

_model = None
_model_key = None


@dataclass
class Transcript:
    text: str
    confidence: float                      # 평균 로그확률(0에 가까울수록 좋음)
    needs_review: bool                     # 임계값 미만 → 사람 확인
    language: str = "ko"
    duration: float = 0.0
    segments: list[dict] = field(default_factory=list)
    model: str = MODEL_SIZE

    def to_dict(self) -> dict:
        return {
            "text": self.text, "confidence": round(self.confidence, 3),
            "needs_review": self.needs_review, "language": self.language,
            "duration": round(self.duration, 2), "model": self.model,
        }


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model(size: str | None = None, device: str | None = None,
               compute_type: str | None = None):
    """모델을 한 번만 로드해 재사용한다(로드가 가장 느린 구간)."""
    global _model, _model_key
    key = (size or MODEL_SIZE, device or DEVICE, compute_type or COMPUTE_TYPE)
    if _model is None or _model_key != key:
        from faster_whisper import WhisperModel
        kwargs = {"device": key[1], "compute_type": key[2]}
        if CPU_THREADS and key[1] == "cpu":
            kwargs["cpu_threads"] = CPU_THREADS
        _model = WhisperModel(key[0], **kwargs)
        _model_key = key
    return _model


def _postprocess(text: str) -> str:
    out = text.strip()
    for a, b in _FIXUPS:
        out = out.replace(a, b)
    return out


def transcribe(audio_path: str, language: str = "ko",
               size: str | None = None, device: str | None = None) -> Transcript:
    """음성 파일 → 텍스트. 확신도가 낮으면 needs_review=True로 사람에게 넘긴다."""
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)

    model = _get_model(size, device)
    segments, info = model.transcribe(
        audio_path, language=language,
        initial_prompt=DOMAIN_PROMPT,
        vad_filter=True,                    # 무음 구간 제거 — 통화 녹음에 유효
        beam_size=5,
    )

    segs, texts, logprobs = [], [], []
    for s in segments:
        texts.append(s.text)
        logprobs.append(s.avg_logprob)
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                     "text": s.text.strip(), "avg_logprob": round(s.avg_logprob, 3)})

    text = _postprocess("".join(texts))
    conf = sum(logprobs) / len(logprobs) if logprobs else -99.0
    return Transcript(
        text=text, confidence=conf,
        needs_review=conf < CONFIDENCE_THRESHOLD or not text,
        language=info.language, duration=info.duration, segments=segs,
        model=size or MODEL_SIZE,
    )
