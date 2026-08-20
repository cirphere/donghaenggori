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

import logging
import os
import re
from dataclasses import dataclass, field

# voice.py 와 같은 로거를 쓴다 — uvicorn 이 루트에 핸들러를 안 달아서
# 자체 이름으로 만들면 INFO 가 조용히 사라진다.
_log = logging.getLogger("uvicorn.error")

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

# 도메인 힌트 — Whisper가 진료과·복지 용어를 더 잘 잡게 한다는 의도였다.
#
# **기본을 끔으로 둔다. hotwords 와 같은 이유로, 같은 일이 실제로 일어났다.**
#
# 바로 아래 hotwords 주석에 적힌 사고("힌트로 준 어휘를 디코더가 신호 약한
# 구간에서 그대로 받아적는다")가 initial_prompt 에서도 똑같이 났다. 그런데
# 이쪽이 더 나쁘다 — hotwords 는 꼬리에 낱말이 붙는 정도였지만, 여기서는
# **발화가 통째로 프롬프트로 바뀐다.**
#
#     정답: 나도 그것은 모르겄네 다보도 많이 #이름#께
#     인식: 정형외과, 내과, 이비인후과, 생활지원사, 동행 매니저.
#
# 전라도 어르신 발화 282건(8kHz 전화 재현, large-v3)으로 켠 전후를 쟀다:
#
#                   CER      프롬프트 누수   CER>0.9 파탄
#     켬(예전 기본) 0.2105       20건           13건
#     끔            0.1744        0건            0건
#
# 누수가 0 이 되고 CER 이 상대 17% 내려간다. 길이와는 무관했다(2.9초~51초에
# 고루 났다) — 그래서 "짧은 파일만 빼기" 같은 가드로는 못 막고, 프롬프트
# 자체를 끄는 것이 답이다.
#
# 목록은 남겨 둔다. STT_DOMAIN_PROMPT=on 으로만 켜지고, **켜기 전에
# tools/stt_eval.py 로 켠 전후 CER 을 재고 나아졌을 때만** 켠다. 그 순서를
# 지키지 않아 hotwords 로 두 번, 여기서 한 번 더 샜다.
_DOMAIN_PROMPT = (
    "병원동행 접수 통화입니다. 정형외과, 내과, 안과, 이비인후과, 치과, 재활의학과, "
    "신경과, 피부과, 보건의료원, 약국, 생활지원사, 사회복지사, 동행 매니저."
)


def domain_prompt() -> str:
    """디코더에 줄 도메인 힌트(initial_prompt). **기본은 끔.**"""
    if (os.environ.get("STT_DOMAIN_PROMPT") or "").strip().lower() != "on":
        return ""
    return _DOMAIN_PROMPT

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
# 이득은 잰 적이 없고 손해는 눈으로 봤다. 그래서 뺐다.
#
# **그리고 짧은 일반 명사도 똑같이 샜다.** 시설명을 뺀 뒤에도 이런 통화가
# 들어왔다 — "…아마 내일 되면 오후면 좋겠어. 보건소 보행기 지팡이". 뒤에
# 붙은 셋은 바로 아래 목록의 꼬리다("… 방문요양 휠체어 보행기 지팡이").
# 고유명사만의 문제가 아니었다. 힌트로 준 어휘는 종류를 가리지 않고, 말이
# 끝난 뒤 남는 무음 구간에서 그대로 붙는다.
#
# 그래서 **기본을 끔으로 둔다.** 목록은 남겨 두되 STT_HOTWORDS=on 으로만
# 켜지고, 켜기 전에 tools/stt_eval.py 로 CER 을 재는 것이 조건이다.
_MOBILITY_TERMS = (
    "배편 여객선 선착장 보건지소 보건진료소 요양병원 한방병원 치과의원 "
    "의원 보건소 복지관 경로당 주간보호센터 방문요양 휠체어 보행기 지팡이"
)


def hotwords() -> str:
    """디코더에 줄 어휘 힌트. **기본은 끔.**

    STT_HOTWORDS=on 으로 켠다. 켜기 전에 tools/stt_eval.py 로 켠 전후 CER 을
    재고, 나아졌을 때만 켠다. 그 순서를 지키지 않아 두 번 샜다.
    """
    if (os.environ.get("STT_HOTWORDS") or "").strip().lower() != "on":
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
    # 실통화에서 관찰된 병원명 오인식.
    #
    # **이쪽이 제일 위험하다.** 병원 이름은 'X병원' 꼴이기만 하면 "원문에서
    # 직접 언급" 으로 잡혀 곧바로 '확인됨' 이 되고 블로커가 0개가 된다 —
    # 없는 병원이 아무 저항 없이 확정까지 간다. 날짜·시각은 틀리면 게이트가
    # 막아 주지만 여기는 막을 것이 없다.
    #
    # 지금은 실재하는 병원 목록이 없어(시설 데이터에 병원 0건) 대조로
    # 걸러낼 수가 없다. 관찰된 것만 손으로 막는다. 제대로 된 처방은 심평원
    # 조회(services/hospital_lookup)로 받은 목록과 대조해 유사한 실재 이름을
    # 후보로 내는 것이고, 그건 재보고 나서 넣는다.
    ("빚병원", "백병원"), ("백제원", "백병원"), ("벡병원", "백병원"),
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


# Whisper 가 무음에 뱉는 자막 상투구.
#
# 실통화 접수 내용이다.
#
#     "…어쨌든 그려. 시청해주셔서 감사합니다. 이거 시청해주셔서 감사합니다."
#
# 어르신이 한 말이 아니다. hotwords 와는 다른 원인이다(그건 이미 껐다) —
# Whisper 학습 데이터에 유튜브 자막이 대량으로 들어 있어서, 말이 끝난 뒤
# 남는 무음 구간을 영상 끝으로 보고 자막 맺음말을 지어낸다. 한국어에서
# 특히 잘 나오는 것이 '시청해주셔서 감사합니다' 다.
#
# **일반적인 인사말은 넣지 않는다.** '감사합니다' 만 지우면 어르신이 통화
# 끝에 실제로 하는 인사를 지운다. 자막에서만 쓰는 말, 접수 통화에서 나올
# 이유가 없는 낱말만 고른다.
_HALLUCINATIONS = (
    "시청해주셔서 감사합니다", "시청해 주셔서 감사합니다",
    "시청해주셔서감사합니다", "끝까지 시청해주셔서 감사합니다",
    "구독과 좋아요", "구독 좋아요", "좋아요와 구독",
    "구독과 알림설정", "알림설정 부탁드립니다",
    "다음 영상에서 만나요", "다음 시간에 만나요", "다음 영상에서 뵙겠습니다",
    "한글자막 by", "자막 제공", "영상 편집",
)


def _strip_hallucinations(text: str) -> str:
    """자막 상투구를 걷어낸다. 지웠으면 로그에 남긴다.

    조용히 지우지 않는다 — 얼마나 자주 나오는지 알아야 다음 수를 정한다.
    """
    out = text
    hit = []
    for phrase in _HALLUCINATIONS:
        if phrase in out:
            hit.append(phrase)
            out = out.replace(phrase, " ")
    if hit:
        # 지운 자리에 남는 구두점·공백을 정리한다.
        out = re.sub(r"\s*([.,])\s*\1+", r"\1", out)
        out = re.sub(r"\s+", " ", out).strip(" .,")
        _log.info("자막 환각 제거 — %s", " / ".join(hit))
    return out


def _postprocess(text: str) -> str:
    out = _strip_hallucinations(text.strip())
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
        initial_prompt=domain_prompt(),
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
