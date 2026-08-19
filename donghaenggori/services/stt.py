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
# 허용 음성 길이 상한(초). 메모리가 음성 길이에 비례해 늘기 때문에 필요하다.
#   실측(small, 2스레드): 3.5초 1,170MB · 3분 1,774MB · 7.5분 2,221MB
#   대략 분당 +140MB. 4GiB 인스턴스에서 15~20분짜리면 OOM으로 프로세스가 죽는다.
#   접수 발화는 수십 초, 매니저 메모도 2분 안쪽이라 5분이면 충분히 넉넉하다.
MAX_SECONDS = float(os.environ.get("STT_MAX_SECONDS", "300"))

# 도메인 힌트 — Whisper가 진료과·복지 용어를 더 잘 잡게 한다
#
# **길게 쓰지 않는다.** initial_prompt 는 224 토큰에서 잘리고, 길수록 프롬프트에
# 있는 단어를 없는데도 받아적는 쪽으로 기운다. 진료과처럼 닫힌 목록만 넣는다.
DOMAIN_PROMPT = (
    "병원동행 접수 통화입니다. 정형외과, 내과, 안과, 이비인후과, 치과, 재활의학과, "
    "신경과, 피부과, 보건의료원, 약국, 생활지원사, 사회복지사, 동행 매니저."
)

# hotwords — "이 단어들이 나올 수 있다" 는 어휘 힌트. initial_prompt 와 다른 인자다.
#
# **관내 시설명·지역명을 넣었다가 뺐다.** 실통화에서 이런 것이 접수 내용으로
# 들어왔다:
#
#     "광주광역시 남구종합사회복지관 상암동구장 전주광역시 장로경합사회복지관"
#
# 어르신이 한 말이 아니다. hotwords 로 넣은 목록("빛고을종합사회복지관
# 광주광역시 동구 무진종합사회복지관 …")을 디코더가 그대로 받아적은 것이다.
# 신호가 약한 구간에서 Whisper 는 힌트로 준 어휘를 없는데도 뱉는 쪽으로 기운다
# — 바로 위 DOMAIN_PROMPT 주석에 적어 둔 위험이 그대로 일어났다. 고유명사는
# 길고 특이해서 한 번 새면 문장 전체가 그것으로 채워진다.
#
# 그것이 **접수 원문(raw_utterance)** 에 남는다. 복지사가 읽는 화면이고
# 파이프라인이 병원·날짜를 뽑는 입력이다. 잘못된 접수가 만들어지는 자리다.
#
# 이득은 잰 적이 없고 손해는 눈으로 봤다. 그래서 뺀다. 다시 넣으려면
# tools/stt_eval.py 로 넣기 전후 CER 을 재고 나서 한다.
#
# 남긴 것은 짧은 일반 명사뿐이다. 고유명사가 아니라서 통째로 새어 나올
# 모양이 아니고, 8kHz 에서 실제로 자주 뭉개지는 어휘다.
_MOBILITY_TERMS = (
    "배편 여객선 선착장 보건지소 보건진료소 요양병원 한방병원 치과의원 "
    "의원 보건소 복지관 경로당 주간보호센터 방문요양 휠체어 보행기 지팡이"
)


def hotwords() -> str:
    """디코더에 줄 어휘 힌트.

    STT_HOTWORDS=off 로 통째로 끌 수 있다. 시연 중에 또 헛말이 보이면
    재배포 없이 환경변수만 바꾸고 재시작하면 된다 — 이 자리에서 무엇을
    더 진단할 시간은 없다.
    """
    if (os.environ.get("STT_HOTWORDS") or "").strip().lower() == "off":
        return ""
    return _MOBILITY_TERMS


# VAD 파라미터 — 기본값은 어르신 발화에 안 맞는다.
#
# faster-whisper 기본 min_silence_duration_ms 는 2000ms 다. 어르신은 문장 중간에
# 그보다 오래 뜸을 들이는 일이 흔해서, 기본값이면 한 문장이 여러 구간으로 잘리고
# 잘린 조각마다 따로 디코딩돼 앞뒤 맥락을 잃는다. 늘려서 한 덩어리로 넘긴다.
#
# speech_pad_ms 는 구간 앞뒤로 남기는 여유다. 기본 400ms 에서는 첫 음절이
# 깎여 "병원" 이 "원" 으로 들어오는 일이 있었다.
VAD_PARAMETERS = {
    "min_silence_duration_ms": 3000,
    "speech_pad_ms": 600,
}

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


class AudioTooLong(ValueError):
    """허용 길이를 넘는 음성. 전사를 시작하기 전에 막는다."""

    def __init__(self, seconds: float, limit: float):
        self.seconds, self.limit = seconds, limit
        super().__init__(
            f"음성이 너무 깁니다 ({seconds/60:.1f}분). 최대 {limit/60:.0f}분까지 처리합니다.")


def probe_duration(audio_path: str) -> float | None:
    """전사 없이 길이만 읽는다. 못 읽으면 None(길이 검사를 건너뛴다)."""
    try:
        import av
        with av.open(audio_path) as c:
            if c.duration:
                return c.duration / 1_000_000
            st = next((s for s in c.streams if s.type == "audio"), None)
            if st is not None and st.duration and st.time_base:
                return float(st.duration * st.time_base)
    except Exception:
        return None
    return None


def transcribe(audio_path: str, language: str = "ko",
               size: str | None = None, device: str | None = None) -> Transcript:
    """음성 파일 → 텍스트. 확신도가 낮으면 needs_review=True로 사람에게 넘긴다.

    긴 음성은 전사 전에 거절한다 — 메모리가 길이에 비례해 늘어,
    막지 않으면 파일 하나로 프로세스가 OOM으로 죽는다.
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)

    dur = probe_duration(audio_path)
    if dur is not None and MAX_SECONDS > 0 and dur > MAX_SECONDS:
        raise AudioTooLong(dur, MAX_SECONDS)

    model = _get_model(size, device)
    segments, info = model.transcribe(
        audio_path, language=language,
        initial_prompt=DOMAIN_PROMPT,
        hotwords=hotwords(),
        vad_filter=True,                    # 무음 구간 제거 — 통화 녹음에 유효
        vad_parameters=VAD_PARAMETERS,
        beam_size=5,
        # **직전 문장을 조건으로 쓰지 않는다.** 기본값(True)은 앞 문장을 다음
        # 구간의 프롬프트로 넣는데, 8kHz 전화 음질에서 한 번 잘못 뜨면 그 오인식이
        # 다음 구간의 힌트가 되어 같은 말을 계속 반복하거나 통화에 없던 문장을
        # 지어낸다. 접수 발화는 수십 초라 문맥으로 얻는 것보다 잃는 게 크다.
        condition_on_previous_text=False,
        # 무음을 말로 잡는 것을 줄인다. 어르신이 한참 뜸을 들이는 구간에서
        # 헛말이 나오면 그 문장이 그대로 접수카드 근거로 올라간다.
        no_speech_threshold=0.6,
        # 무음에 대고 문장을 지어내면 그 구간을 버린다. 기본값은 꺼짐(None)인데,
        # 어르신이 한참 뜸을 들이는 통화라 켜 둘 값어치가 있다. 초 단위다.
        hallucination_silence_threshold=2.0,
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
