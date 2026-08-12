"""전화 연동 — ClawOps VoiceML 웹훅.

**AI가 어르신과 대화하지 않는다.** 고정 인사만 하고 그다음은 듣기만 한다.
ClawOps 에는 AI Voice Agent SDK(OpenAI Realtime 이 통화를 주도)도 있지만 쓰지 않았다.
대화를 LLM 이 끌면 우리 긴급 분류기가 모든 발화를 보지 못하고, LLM 이 도구를
불러줄 때만 안전장치가 도는 구조가 된다. 어르신의 건강 발화가 외부 LLM 으로
나가는 문제도 생긴다. VoiceML 은 그 둘 다 피한다.

    ① 전화 수신    → /api/voice/incoming
                     <Say> 인사 → <Record> 녹음
    ② 녹음 끝      → /api/voice/recording
                     녹음 내려받기 → STT → 접수 파이프라인
                     긴급이면  <Dial> 로 담당자에게 즉시 연결
                     아니면    <Say> 안내 후 종료

②의 콜백은 **어르신이 아직 통화 중일 때** 불린다. 그래서 긴급 전환이 통화
중에 이뤄진다 — 녹음을 나중에 배치로 훑는 방식이었다면 못 했을 일이다.

미설정 상태에서도 임포트되고 라우트는 뜬다. 서명 키가 없으면 요청을 거절한다
(열어두고 조용히 통과시키는 것보다 낫다 — 이 엔드포인트는 공개돼야 한다).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import tempfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..core import pipeline

router = APIRouter(prefix="/api/voice", tags=["전화(ClawOps)"])

# 대시보드 > Settings > Webhook 의 Signing Key. API Key 와 다른 값이다.
SIGNING_KEY = os.environ.get("CLAWOPS_SIGNING_KEY", "")
# 긴급 시 연결할 담당자 번호. 없으면 전환하지 않고 안내만 한다.
STAFF_NUMBER = os.environ.get("CLAWOPS_STAFF_NUMBER", "")
# 녹음 최대 길이(초). 길게 잡으면 STT 대기가 길어져 어르신이 침묵을 견뎌야 한다.
MAX_RECORD_SECONDS = int(os.environ.get("CLAWOPS_MAX_RECORD_SECONDS", "45"))

GREETING = ("안녕하세요, 동행고리 인공지능 서비스입니다. "
            "어느 병원에 언제 가시는지 말씀해 주세요. "
            "다 말씀하시면 그대로 끊으셔도 됩니다. 담당자가 확인하고 연락드립니다. "
            "많이 아프시거나 급한 상황이면 지금 끊고 119에 전화해 주세요.")


def _xml(body: str) -> Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{body}</Response>',
                    media_type="application/xml")


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def _verify(request: Request) -> dict:
    """X-Signature 검증 후 폼 파라미터를 돌려준다.

    서명: base64(HMAC-SHA256(signingKey, URL + 정렬된 key+value 이어붙이기))
    키가 없으면 검증할 방법이 없으므로 거절한다. 이 엔드포인트는 인터넷에
    열려 있어야 하고, 열어둔 채로 두면 누구나 접수를 만들 수 있다.
    """
    form = dict(await request.form())
    if not SIGNING_KEY:
        raise HTTPException(503, "CLAWOPS_SIGNING_KEY 미설정 — 전화 연동이 꺼져 있습니다")

    data = str(request.url) + "".join(f"{k}{v}" for k, v in sorted(form.items()))
    expected = base64.b64encode(
        hmac.new(SIGNING_KEY.encode(), data.encode(), hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(request.headers.get("X-Signature", ""), expected):
        raise HTTPException(401, "서명이 올바르지 않습니다")
    return form


@router.post("/incoming")
async def incoming(request: Request) -> Response:
    """전화가 걸려왔다. 인사하고 녹음을 시작한다. 대화는 하지 않는다."""
    await _verify(request)
    action = str(request.url_for("voice_recording"))
    return _xml(
        f'<Say language="ko">{_esc(GREETING)}</Say>'
        f'<Record maxLength="{MAX_RECORD_SECONDS}" playBeep="true" '
        f'finishOnKey="#" action="{_esc(action)}"/>'
    )


@router.post("/recording", name="voice_recording")
async def recording(request: Request) -> Response:
    """녹음이 끝났다. 어르신은 아직 통화 중이다.

    여기서 STT → 접수까지 끝내고, 긴급이면 그 자리에서 담당자로 넘긴다.
    실패해도 통화를 끊지 않는다 — 사람이 확인하겠다고 안내하고 종료한다.
    """
    form = await _verify(request)
    phone = form.get("From") or ""
    url = form.get("RecordingUrl") or ""
    duration = float(form.get("RecordingDuration") or 0)

    if not url or duration <= 0:
        # 인사만 듣고 끊었거나 녹음이 비었다. 접수를 만들지 않는다.
        return _xml('<Say language="ko">말씀이 녹음되지 않았습니다. '
                    '다시 걸어주시거나 담당자에게 연락 주세요.</Say><Hangup/>')

    try:
        text = _transcribe_url(url)
    except Exception:
        return _xml('<Say language="ko">지금 처리가 어렵습니다. '
                    '담당자가 확인 후 연락드리겠습니다.</Say><Hangup/>')

    if not text.strip():
        return _xml('<Say language="ko">말씀을 알아듣지 못했습니다. '
                    '담당자가 확인 후 연락드리겠습니다.</Say><Hangup/>')

    res = pipeline.run(phone, text, channel="전화")
    _save(res, phone, text)

    if res.urgent:
        # 통화 중이므로 지금 넘긴다. 우리가 응급 여부를 판정하는 것이 아니라,
        # 사람에게 넘길 이유가 생겼다는 뜻이다.
        if STAFF_NUMBER:
            return _xml(
                '<Say language="ko">담당자에게 바로 연결해 드리겠습니다. '
                '잠시만 기다려 주세요.</Say>'
                f'<Dial timeout="30"><Number>{_esc(STAFF_NUMBER)}</Number></Dial>'
                '<Say language="ko">연결이 어렵습니다. 급하시면 119에 전화해 주세요.</Say>')
        return _xml('<Say language="ko">담당자가 바로 연락드리겠습니다. '
                    '급하시면 119에 전화해 주세요.</Say><Hangup/>')

    c = res.card
    when = c.date_label or "말씀하신 날짜"
    where = c.hospital if c.hospital_status == "확인됨" else None
    said = f"{when} {where}" if where else when
    return _xml(
        f'<Say language="ko">{_esc(said)}로 접수했습니다. '
        '담당자가 확인한 뒤 연락드리겠습니다. 감사합니다.</Say><Hangup/>')


def _transcribe_url(url: str) -> str:
    """녹음(24시간 유효 서명 URL)을 내려받아 전사한다. WAV PCM 16bit mono 8kHz."""
    import httpx

    from ..services import stt
    with httpx.Client(timeout=20.0) as cli:
        audio = cli.get(url).content
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    try:
        tmp.write(audio)
        tmp.close()
        return stt.transcribe(tmp.name).text
    finally:
        os.unlink(tmp.name)


def _save(res, phone: str, text: str) -> None:
    """접수 기록을 남긴다. 실패해도 통화 흐름을 막지 않는다."""
    from ..core import db
    try:
        if res.urgent:
            class _Stub:
                target = res.profile["name"] if res.profile else "미확인"
                raw_utterance = text
                intent = "긴급"
                hospital = hospital_status = dept = None
                date_value = date_label = need_level = None
            db.save_intake(_Stub(), phone, "전화", status="긴급")
        elif res.card:
            db.save_intake(res.card, phone, "전화",
                           status="임시 접수" if res.profile is None else "접수 대기")
    except Exception:
        pass
