"""전화 연동 — ClawOps VoiceML 웹훅.

**AI 는 묻고 받아적기만 한다.** 확인도 등록도 하지 않는다.

ClawOps 에는 AI Voice Agent SDK(OpenAI/Gemini Realtime 이 통화를 주도)도 있지만
쓰지 않았다. 대화를 LLM 이 끌면 우리 긴급 분류기가 모든 발화를 보지 못하고,
LLM 이 도구를 불러줄 때만 안전장치가 도는 구조가 된다. 어르신의 건강 발화가
외부 LLM 으로 나가는 문제도 생긴다. VoiceML 은 그 둘 다 피한다.

    1턴  /incoming   <Say> 인사 → <Record> 요청 내용
    ──   /recording  내려받기 → STT → 파이프라인
         · 긴급          → <Dial> 담당자 (2턴으로 가지 않는다)
         · 대상자 후보    → <Say> "박순자 님 맞으실까요?" → <Record>
         · 미등록        → <Say> "성함과 읍면동을 말씀해 주세요" → <Record>
    2턴  /confirm    답변 STT → 접수에 **원문 그대로** 첨부 → 안내 후 종료

녹음 콜백이 **통화 중에** 불린다. 그래서 긴급 전환이 통화 도중에 이뤄진다 —
녹음을 나중에 배치로 훑는 방식이었다면 못 했을 일이다.

2턴 답변은 해석하지 않는다. 후보 이름이 답변에 들어 있는지 **문자열 대조만**
하고, 그 결과로 상태를 조정한 뒤 원문을 그대로 남긴다. 대조는 판단이 아니라서
근거를 물으면 그대로 보여줄 수 있다. 확정은 사회복지사가 한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import tempfile
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

import logging

from ..core import db, pipeline

# uvicorn 로거를 빌려 쓴다. 자체 이름으로 만들면 루트로 propagate 되는데
# uvicorn 은 루트에 핸들러를 달지 않아 INFO 가 조용히 사라진다(WARNING 만
# 파이썬 최후 핸들러로 겨우 나온다). 전화 연동은 로그가 유일한 단서라
# 확실히 보이는 쪽을 택한다.
_log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/voice", tags=["전화(ClawOps)"])

# 대시보드 > Settings > Webhook 의 Signing Key. API Key 와 다른 값이다.
SIGNING_KEY = os.environ.get("CLAWOPS_SIGNING_KEY", "")
# 긴급 시 연결할 담당자 번호. 없으면 전환하지 않고 안내만 한다.
STAFF_NUMBER = os.environ.get("CLAWOPS_STAFF_NUMBER", "")
# 녹음 상한(초).
#
# 한때 15초까지 줄였다. 키가 안 먹던 시절에는 이 값이 곧 어르신이 침묵 속에서
# 기다리는 시간이어서, 말이 잘릴 위험보다 대기가 더 아팠기 때문이다.
#
# 키가 먹는 것을 확인한 뒤 1분으로 늘렸다. 이제 어르신이 말을 마치고 아무 키나
# 누르면 그 자리에서 끝나므로, 상한은 '대기 시간'이 아니라 '천천히 말해도 되는
# 여유'가 된다. 어르신은 병원 이름을 떠올리는 데 시간이 걸리고, 15초에서 말이
# 잘리면 접수 자체가 반쪽이 된다.
MAX_RECORD_SECONDS = int(os.environ.get("CLAWOPS_MAX_RECORD_SECONDS", "60"))

# 녹음을 끝내는 키. 기본은 **아무 키나**다.
#
# 처음에는 "#" 하나만 받았는데, 실통화에서 눌러도 넘어가지 않았다. 어르신이
# 통화 중에 폰을 떼고 정확히 우물 정자를 찾아 누르는 것 자체가 어렵고, 회선에
# 따라 DTMF 가 제대로 전달되지 않기도 한다. 아무 키나 받으면 잘못 눌러도 넘어간다.
#
# 한동안 어떤 키도 먹지 않아 회선 문제로 의심했는데, 원인은 다른 데 있었다
# (DEMO_CALLER_PHONE 미설정 → 미등록 분기). 실통화에서 정상 동작을 확인했다.
#
# 그래서 안내에 키를 다시 넣었다. 무음 종료가 없는 이상 키가 유일한 종료
# 수단이고, 이게 먹어야 상한을 1분까지 늘려도 어르신이 기다리지 않는다.
FINISH_ON_KEY = os.environ.get("CLAWOPS_FINISH_ON_KEY", "1234567890*#")
# 같은 번호의 재전화를 중복 후보로 표시할 시간 범위(분)
DUPLICATE_WINDOW_MIN = int(os.environ.get("CLAWOPS_DUPLICATE_WINDOW_MIN", "10"))

# 통화 앞에서 누른 번호 → 접수에 남길 답변과 상태.
# '확인됨' 은 쓰지 않는다. 버튼을 눌렀다는 것이 본인이라는 증거는 아니다.
_IDENTITY_ANSWER = {
    "self": ("1번(본인 맞다고 응답)", "추정"),
    "other": ("2번(본인이 아니라고 응답) — 성함·주소를 말로 남김", "확인 필요"),
    "unknown": ("응답 없음(키 입력 없이 진행)", "확인 필요"),
    # 등록된 대상자가 아니다. 이름을 물을 수 없어 확인 질문 자체를 건너뛰었고,
    # 대신 성함·읍면동을 말로 받았다. 대상자 등록은 복지사가 판단한다.
    "new": ("미등록 번호 — 성함·읍면동을 말로 남김 · 대상자 등록 필요", "확인 필요"),
}

# 시연 전용 — 발표자가 자기 폰으로 걸었을 때 등록된 대상자로 조회되게 한다.
# 이게 없으면 본선에서 시연 통화가 전부 '신규 대상자(미등록 번호)'로 뜬다.
# **본인확인이 아니다.** 조회 키를 바꿔 끼우는 것뿐이고, 대상자 확정은
# 여느 통화와 똑같이 사회복지사가 한다. 두 값이 다 있어야 동작한다.
DEMO_CALLER_PHONE = os.environ.get("DEMO_CALLER_PHONE", "")
DEMO_CALLER_TARGET = os.environ.get("DEMO_CALLER_TARGET", "")

# 우리 서비스의 공개 주소. 2턴 콜백(<Record action=...>)을 만들 때 쓴다.
#
# request.url_for 만 쓰면 http:// 가 나온다 — nginx 가 app:8000 에 http 로
# 붙기 때문이다. 그 주소를 ClawOps 에 주면 Cloudflare 가 301 로 https 에
# 돌리고, POST 리다이렉트에서 본문이 날아가 확인 단계가 통째로 깨진다.
# 헤더를 믿는 대신 공개 주소를 직접 적어두는 편이 확실하다.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# 인사말은 짧아야 한다. 어르신이 20초짜리 안내를 끝까지 듣지 않는다 — 실제로
# 인사 도중 끊겨서 녹음이 시작조차 못 한 통화가 있었다.
#
# **"다 말씀하시면 어떻게 하세요"를 반드시 넣는다.** <Record> 에는 침묵 감지가
# 없어서(maxLength·finishOnKey·playBeep·action 뿐) 말을 마쳐도 저절로 끝나지
# 않는다. 안내가 없으면 어르신은 상한(1분)까지 기다리거나 끊어 버린다.
#
# 시연장에서 문구를 다듬을 수 있도록 환경변수로 뺐다.
# 미등록 번호용 인사. 등록된 번호는 이름을 확인하는 문장이 따로 나간다.
#
# **성함과 읍면동을 먼저 묻는다.** 예전에는 미등록 번호에도 증상만 물었고,
# 그러면 복지사에게 "누구인지 모르는 접수"가 남아 발신번호로 되걸어 보는 수밖에
# 없었다. 처음 연락한 어르신일수록 놓치면 안 되는 쪽이다.
#
# 여기서 받은 이름으로 대상자를 **자동 등록하지는 않는다.** 대상자 자격은
# 장기요양등급 같은 근거로 정해지고 발신번호는 본인확인이 못 된다(남의 폰·
# 공중전화). 게다가 이름·주소가 STT 로 들어와 오인식될 수 있다. 통화는 받아만
# 적고, 등록은 사회복지사가 확인한 뒤에 한다.
#
# 119 안내는 넣지 않는다. 접수 전화에 대고 끊으라고 하는 것이 어색하고,
# 안내가 길어지면 어르신이 끝까지 듣지 않는다. 긴급은 발화에서 감지해
# 담당자로 넘기고, **전환이 실패했을 때만** 119 를 안내한다.
# 말을 마쳤을 때 어떻게 하는지. 세 안내가 모두 같은 문장을 쓰도록 묶어둔다 —
# 한 군데만 고쳐서 어긋나면, 어르신은 안내받은 대로 했는데 반응이 없게 된다.
#
# 무음 종료가 없으므로 키가 유일한 종료 수단이다. 안 누르면 상한까지 간다.
DONE_HINT = "마치시면 아무 번호나 눌러 주세요."

GREETING = os.environ.get("CLAWOPS_GREETING") or (
    "동행고리입니다. 처음 연락 주셨네요. 삐 소리 후 어르신 성함과 사시는 읍면동, "
    f"그리고 어느 병원에 언제 가시는지 말씀해 주세요. {DONE_HINT}")

BYE = "담당자가 확인한 뒤 연락드리겠습니다. 감사합니다."

# 본인 확인을 먼저 물을지. 회선이 DTMF 를 전달하지 않으면 어차피 흘러가지만,
# 시연장에서 아예 끄고 싶을 때를 위해 남긴다.
ASK_IDENTITY = os.environ.get("CLAWOPS_ASK_IDENTITY", "1").strip() not in ("0", "false", "no")

SYMPTOM_PROMPT = ("어디가 편찮으신지, 어느 병원에 언제 가시는지 말씀해 주세요. "
                  + DONE_HINT)
OTHER_PROMPT = ("어르신 성함과 사시는 읍면동, 그리고 어느 병원에 언제 가시는지 "
                "말씀해 주세요. " + DONE_HINT)


# ─────────────────────────────────────────────── VoiceML 만들기 --

def _callback(request: Request, name: str) -> str:
    """VoiceML 에 넣을 콜백 주소. PUBLIC_BASE_URL 이 있으면 그것을 쓴다."""
    path = request.url_for(name).path
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{path}"
    # 설정이 없으면 요청에서 유추한다. 프록시 뒤에서는 scheme 이 틀릴 수 있어
    # X-Forwarded-Proto 를 우선한다.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{request.url.netloc}{path}"


def _xml(body: str) -> Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{body}</Response>',
                    media_type="application/xml")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _say(text: str) -> str:
    return f'<Say language="ko">{_esc(text)}</Say>'


def _record(action: str) -> str:
    return (f'<Record maxLength="{MAX_RECORD_SECONDS}" playBeep="true" '
            f'finishOnKey="{_esc(FINISH_ON_KEY)}" action="{_esc(action)}"/>')


def _hangup(text: str) -> Response:
    return _xml(_say(text) + "<Hangup/>")


# ClawOps 가 주는 Dial 결과 → 우리 표기
_DIAL_STATUS = {
    "completed": "연결됨", "busy": "통화중", "no-answer": "응답없음",
    "failed": "실패", "canceled": "취소됨",
}


def _transfer(request: Request, reason: str, intake_id: int | None = None) -> Response:
    """긴급 — 통화 중에 담당자로 넘긴다. 응급 여부를 우리가 판정하는 것이 아니라,
    사람에게 넘길 이유가 생겼다는 뜻이다.

    **결과를 반드시 받는다.** 담당자가 못 받은 것을 아무도 모르는 상태가 제일
    위험하다 — 어르신은 안내를 듣고 끊는데 시스템에는 기록이 없어 아무도 다시
    걸지 않는다. <Dial action> 으로 연결 여부를 돌려받아 접수에 남긴다.
    """
    if not STAFF_NUMBER:
        return _hangup(f"{reason} 담당자가 바로 연락드리겠습니다. 급하시면 119에 전화해 주세요.")
    action = _callback(request, "voice_dial_result")
    if intake_id:
        action += f"?intake={intake_id}"
    return _xml(
        _say(f"{reason} 담당자에게 바로 연결해 드리겠습니다. 잠시만 기다려 주세요.")
        + f'<Dial timeout="30" action="{_esc(action)}">'
        f'<Number>{_esc(STAFF_NUMBER)}</Number></Dial>')


@router.post("/status", name="voice_status")
async def call_status(request: Request) -> Response:
    """통화 상태 알림 — 번호 설정의 '통화 상태 webhook URL' 이 여기로 온다.

    통화 흐름을 지시하지 않는다(VoiceML 을 돌려줄 자리가 아니다). 기록만 한다.

    받을 이벤트 중 실제로 쓰는 것은 **호전환 결과**다. <Dial action> 은 전환이
    끝나야 오지만 이쪽은 실시간이고, action 콜백이 어떤 이유로 우리에게 닿지
    못해도 백업이 된다. 담당자가 못 받은 것을 아무도 모르는 상태만은 피해야 한다.

    나머지(발신 시작·벨 울림·응답·종료)는 감사 로그에만 남긴다.
    """
    form = await _verify(request)
    event = (form.get("CallStatus") or form.get("StatusCallbackEvent") or "").strip()
    raw = (form.get("DialCallStatus") or "").strip().lower()

    if raw:
        status = _DIAL_STATUS.get(raw, raw)
        iid = _recent_urgent_intake(form.get("From") or "")
        if iid:
            try:
                db.set_transfer_status(iid, status)
            except Exception:
                pass
    else:
        try:
            db.log_audit("전화 시스템", "시스템", "통화상태", "call",
                         form.get("CallId") or "", event)
        except Exception:
            pass
    # 상태 알림은 통화를 지시하지 않는다 — 빈 응답으로 끝낸다
    return Response(status_code=204)


def _recent_urgent_intake(phone: str) -> int | None:
    """이 번호의 가장 최근 긴급 접수. 상태 알림에는 intake 를 실을 수 없어서
    번호로 되짚는다. 통화 한 건이 진행 중인 동안에는 이것으로 충분하다."""
    if not phone:
        return None
    try:
        rows = db.recent_intakes(phone, minutes=30)
    except Exception:
        return None
    for r in rows:
        if r.get("status") in ("긴급", "긴급 처리됨"):
            return r["id"]
    return None


@router.post("/dial-result", name="voice_dial_result")
async def dial_result(request: Request) -> Response:
    """긴급 전환이 끝났다. 연결됐는지 기록하고, 실패면 119 안내로 마무리한다."""
    form = await _verify(request)
    raw = (form.get("DialCallStatus") or "").strip().lower()
    status = _DIAL_STATUS.get(raw, raw or "알 수 없음")
    try:
        intake_id = int(request.query_params.get("intake") or 0)
    except ValueError:
        intake_id = 0
    if intake_id:
        try:
            db.set_transfer_status(intake_id, status)
        except Exception:
            pass
    if raw == "completed":
        return _xml("<Hangup/>")
    return _hangup("담당자와 연결하지 못했습니다. "
                   "급하시면 119에 전화해 주시고, 담당자가 다시 연락드리겠습니다.")


# ─────────────────────────────────────────────────── 서명 검증 --

def _sign(data: str) -> str:
    return base64.b64encode(
        hmac.new(SIGNING_KEY.encode(), data.encode(), hashlib.sha256).digest()).decode()


def _url_candidates(request: Request) -> list[str]:
    """서명 대상이 됐을 법한 URL 표기들.

    보내는 쪽은 '대시보드에 등록된 주소' 로 서명하는데, 받는 쪽에서 그 문자열을
    정확히 복원하기가 은근히 어렵다 — 프록시를 거치며 scheme 이 바뀌고,
    query 를 포함했는지도 알 수 없다. 그래서 후보를 모아 하나라도 맞으면 통과시킨다.
    맞은 후보는 로그에 남겨, 확인되면 하나로 좁힐 수 있게 한다.
    """
    u = request.url
    path_q = u.path + (f"?{u.query}" if u.query else "")
    out = [str(u), f"{u.scheme}://{u.netloc}{u.path}"]
    if PUBLIC_BASE_URL:
        out += [f"{PUBLIC_BASE_URL}{path_q}", f"{PUBLIC_BASE_URL}{u.path}"]
    # 프록시 뒤에서 scheme 이 뒤집혀 계산될 수 있다
    for base in list(out):
        out.append(base.replace("https://", "http://", 1) if base.startswith("https://")
                   else base.replace("http://", "https://", 1))
    return list(dict.fromkeys(out))


async def _verify(request: Request) -> dict:
    """X-Signature 검증 후 파라미터를 돌려준다.

    서명: base64(HMAC-SHA256(signingKey, data)) — data 는 URL 에 정렬된
    key+value 를 이어붙인 것이다. 키가 없으면 검증할 방법이 없으므로 거절한다.
    이 엔드포인트는 인터넷에 열려 있어야 하고, 열어둔 채로 두면 누구나 접수를
    만들 수 있다.
    """
    raw = await request.body()
    try:
        form = dict(await request.form())
    except Exception:
        form = {}
    if not form and raw:
        # form 이 아니라 JSON 으로 오는 경우도 대비한다
        try:
            import json as _json
            parsed = _json.loads(raw.decode())
            if isinstance(parsed, dict):
                form = {k: str(v) for k, v in parsed.items()}
        except Exception:
            pass

    if not SIGNING_KEY:
        raise HTTPException(503, "CLAWOPS_SIGNING_KEY 미설정 — 전화 연동이 꺼져 있습니다")

    got = (request.headers.get("X-Signature") or "").strip()
    if got.lower().startswith("sha256="):        # 접두사를 붙여 보내는 구현도 있다
        got = got.split("=", 1)[1].strip()
    if not got:
        raise HTTPException(401, "X-Signature 헤더가 없습니다")

    # 서명 대상은 **URL 과 파라미터를 함께 묶은 것**만 인정한다.
    #
    # 401 원인을 찾을 때 URL 만·본문만·파라미터만도 시도하게 넓혔었는데, 그건
    # 검증이 아니었다. URL 만 서명하면 본문이 무엇이든 통과하므로, 유효한 서명을
    # 한 번 확보하면 From·RecordingUrl 을 바꿔 가짜 접수를 만들고 우리가 임의
    # 주소를 내려받게 할 수 있다(_transcribe_url 이 그 URL 을 그대로 가져온다).
    # 파라미터만 서명하면 같은 서명을 다른 엔드포인트에 재사용할 수 있다.
    #
    # 실통화에서 'url+params' 로 맞는 것이 확인됐으니 나머지는 되돌린다.
    # URL 표기 후보는 남긴다 — 프록시를 거치며 scheme 이 바뀔 수 있고, 각
    # 후보가 파라미터를 함께 묶으므로 위 문제가 없다.
    joined = "".join(f"{k}{v}" for k, v in sorted(form.items()))
    for url in _url_candidates(request):
        if hmac.compare_digest(got, _sign(url + joined)):
            # 성공은 남기지 않는다. 통화 한 건에 여러 번 찍혀 시끄럽고,
            # 기준(url+params)은 실통화로 확정됐다. 문제만 아래에 남긴다.
            return form

    # 이 로그가 유일한 단서다. 보내는 쪽이 어떤 URL 로 서명했는지 알 수 없으니,
    # 우리가 복원한 URL 들을 그대로 남겨 대조할 수 있게 한다. 키는 넣지 않는다.
    _log.warning(
        "서명 불일치 (기준: URL + 정렬된 파라미터)\n"
        "  받은 서명   : %s…\n  파라미터 키 : %s\n  시도한 URL  :\n%s",
        got[:12], sorted(form),
        "\n".join(f"    - {u}" for u in _url_candidates(request)))
    raise HTTPException(401, "서명이 올바르지 않습니다")


# ──────────────────────────────────────────────────── 1턴 수신 --

@router.post("/incoming")
async def incoming(request: Request) -> Response:
    """전화가 걸려왔다.

    등록된 번호면 **먼저 본인부터 확인하고** 증상을 받는다.

        "박순자 님 맞으신가요? 맞으면 1번, 아니면 2번을 눌러 주세요."
           1번 → "어디가 편찮으신지 말씀해 주세요"
           2번 → "성함과 사시는 읍면동을 말씀해 주세요"

    순서가 중요하다. 예전에는 녹음을 먼저 받고 STT 를 마친 뒤에 본인을 되물었는데,
    그 대기 사이에 통화가 끊겨 확인 질문이 들리지 않았다. 확인을 앞으로 옮기면
    STT 대기가 통화 맨 끝(접수 안내)으로 밀린다 — 거기서 잘려도 접수는 이미
    저장돼 있어 잃는 것이 없다.

    키를 못 누르거나 회선이 DTMF 를 전달하지 않으면 <Gather> 는 콜백 없이
    끝나고, 아래 <Say>·<Record> 로 흘러간다. 그때는 예전과 같은 1턴 흐름이다.
    """
    form = await _verify(request)
    raw = form.get("From") or ""
    lookup = _lookup_phone(raw)
    prof = db.get_profile(lookup)
    # 프로필을 못 찾으면 이름을 물을 수 없어 미등록 안내로 간다. 왜 못 찾았는지
    # 로그에 남긴다 — 실통화에서 이름 확인이 안 나와 한참 헤맸다. 대개
    # DEMO_CALLER_PHONE 미설정이거나 번호 표기가 다른 경우다.
    _log.info("발신 %s → 조회 %s → %s", raw, db.normalize_phone(lookup),
              prof["name"] if prof else "등록 없음(미등록 안내로 진행)")
    def ask(who: str) -> str:
        return _record(_callback(request, "voice_recording") + f"?who={who}")

    # 미등록 번호 — 이름을 모르니 확인할 것도 없다. 성함·읍면동부터 받는다.
    if not prof:
        return _xml(_say(GREETING) + ask("new"))

    # 등록된 번호인데 확인 질문을 꺼둔 경우. 이름은 이미 아니까 증상만 받는다.
    if not ASK_IDENTITY:
        return _xml(_say(SYMPTOM_PROMPT) + ask("unknown"))

    # 확인 문구는 <Gather> **안에** 둔다. 문서상 그래야 barge-in 이 걸린다 —
    # 문장이 끝나기 전에 눌러도 재생이 끊기고 그 키가 입력으로 잡힌다.
    #
    # 한때 밖으로 뺐던 적이 있다. 실통화에서 이 문구가 안 들리고 /identity 도
    # 안 찍혀서 중첩이 원인인 줄 알았는데, 실제로는 DEMO_CALLER_PHONE 이 비어
    # 있어 미등록 분기로 빠진 것이었다 — <Gather> 자체가 응답에 없었다.
    # 안 들린 것과 중첩은 무관했다.
    return _xml(
        f'<Gather numDigits="1" timeout="7" '
        f'action="{_esc(_callback(request, "voice_identity"))}">'
        + _say(f"동행고리입니다. {prof['name']} 님 맞으신가요? "
               "맞으시면 1번, 아니시면 2번을 눌러 주세요.")
        + '</Gather>'
        # 키를 못 눌렀다 — 묻지 말고 바로 증상을 받는다. 대상자는 확인 필요로 남는다.
        + _say(SYMPTOM_PROMPT) + ask("unknown"))


@router.post("/identity", name="voice_identity")
async def identity(request: Request) -> Response:
    """1번(본인) / 2번(아니오) 응답. 어느 쪽이든 다음은 녹음이다."""
    form = await _verify(request)
    digit = (form.get("Digits") or "").strip()
    who = "self" if digit == "1" else "other" if digit == "2" else "unknown"
    action = _callback(request, "voice_recording") + f"?who={who}"
    prompt = SYMPTOM_PROMPT if who == "self" else OTHER_PROMPT
    return _xml(_say(prompt) + _record(action))


def _lookup_phone(raw: str) -> str:
    """조회에 쓸 번호. 시연용 매핑이 걸려 있으면 바꿔 끼운다."""
    if DEMO_CALLER_PHONE and DEMO_CALLER_TARGET:
        if db.normalize_phone(raw) == db.normalize_phone(DEMO_CALLER_PHONE):
            return DEMO_CALLER_TARGET
    return raw


@router.post("/recording", name="voice_recording")
async def recording(request: Request) -> Response:
    """요청 내용 녹음이 끝났다. 어르신은 아직 통화 중이다.

    **여기서 오래 끌면 통화가 끊긴다.** 내려받기 + STT 를 마쳐야 다음 안내를
    돌려줄 수 있는데, 보내는 쪽이 응답을 얼마나 기다려 주는지 문서에 없다.
    실통화에서 확인 질문이 들리지 않은 적이 있어 단계별 시간을 남긴다.
    """
    t0 = time.monotonic()
    form = await _verify(request)
    phone = _lookup_phone(form.get("From") or "")

    text = _read_recording(form)
    _log.info("녹음 처리 %.1f초 소요", time.monotonic() - t0)
    if text is None:
        return _hangup("말씀이 녹음되지 않았습니다. 다시 걸어주시거나 담당자에게 연락 주세요.")
    if not text:
        return _hangup("말씀을 알아듣지 못했습니다. 담당자가 확인 후 연락드리겠습니다.")

    res = pipeline.run(phone, text, channel="전화")
    if res.urgent:
        return _transfer(request, "긴급한 상황으로 보입니다.", _save(res, phone, text))

    intake_id = _save(res, phone, text)

    # 통화 앞에서 받은 1번/2번 응답을 접수에 남긴다. 눌렀다는 사실이 근거일 뿐
    # 본인확인이 아니다 — 남의 폰으로 건 사람도 1번을 누를 수 있다. 최대 '추정'
    # 이고 확정은 사회복지사가 한다.
    if intake_id:
        who = (request.query_params.get("who") or "unknown").strip()
        answer, status = _IDENTITY_ANSWER.get(who, _IDENTITY_ANSWER["unknown"])
        try:
            db.attach_identity_answer(intake_id, answer, status)
        except Exception:
            pass

    return _hangup(f"{_receipt(res)} {BYE}")


# ──────────────────────────────────────────────────── 2턴 확인 --

# ─────────────────────────────────────────────────────── 도우미 --

def _read_recording(form: dict) -> str | None:
    """녹음을 내려받아 전사한다. None=녹음 없음, ""=전사 실패.

    **실패를 조용히 삼키지 않는다.** 예전에는 예외를 그냥 먹어서, 통화가
    "지금 처리가 어렵습니다" 로 끝나도 왜 그런지 알 방법이 없었다. 로그가
    유일한 단서인 경로다.
    """
    url = form.get("RecordingUrl") or ""
    try:
        duration = float(form.get("RecordingDuration") or 0)
    except ValueError:
        duration = 0
    if not url or duration <= 0:
        _log.warning("녹음 없음 — RecordingDuration=%s · URL %s",
                     form.get("RecordingDuration"), "있음" if url else "없음")
        return None
    try:
        text = _transcribe_url(url).strip()
    except Exception as e:
        _log.warning("녹음 처리 실패 (%.1f초) — %s: %s", duration, type(e).__name__, e)
        return ""
    # 전사 결과를 남긴다. 전화 음질(8kHz)에서 무엇이 들리는지가 지금 가장 큰
    # 미지수라 확인이 끝날 때까지 둔다. 실제 개인정보를 다루게 되면 지울 것.
    _log.info("전사 (%.1f초, %d자): %s", duration, len(text), text or "(빈 문자열)")
    return text


def _transcribe_url(url: str) -> str:
    """녹음(24시간 유효 서명 URL)을 내려받아 전사한다. WAV PCM 16bit mono 8kHz."""
    import httpx

    from ..services import stt
    t0 = time.monotonic()
    with httpx.Client(timeout=20.0) as cli:
        resp = cli.get(url)
    dl = time.monotonic() - t0
    if resp.status_code != 200:
        raise RuntimeError(f"녹음 내려받기 HTTP {resp.status_code}")
    audio = resp.content
    if not audio:
        raise RuntimeError("녹음 파일이 비어 있음")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    try:
        tmp.write(audio)
        tmp.close()
        t1 = time.monotonic()
        text = stt.transcribe(tmp.name).text
        _log.info("내려받기 %.1f초 · 전사 %.1f초 (%d바이트)",
                  dl, time.monotonic() - t1, len(audio))
        return text
    finally:
        os.unlink(tmp.name)


def _receipt(res) -> str:
    c = res.card
    if c is None:
        return "접수했습니다."
    when = c.date_label or "말씀하신 날짜"
    if c.hospital and c.hospital_status == "확인됨":
        return f"{when} {c.hospital}으로 접수했습니다."
    return f"{when}로 접수했습니다."


def _save(res, phone: str, text: str) -> int | None:
    """접수 기록을 남긴다. 실패해도 통화 흐름을 막지 않는다.

    같은 번호로 방금 접수가 있으면 **합치지 않고 표시만** 한다. 어르신이 정말
    두 번 요청했을 수도 있어서, 묶는 판단은 사회복지사에게 남긴다.
    """
    try:
        if res.urgent:
            class _Stub:
                target = res.profile["name"] if res.profile else "미확인"
                raw_utterance = text
                intent = "긴급"
                hospital = hospital_status = dept = None
                date_value = date_label = need_level = None
            return db.save_intake(_Stub(), phone, "전화", status="긴급")
        if not res.card:
            return None
        prior = db.recent_intakes(phone, minutes=DUPLICATE_WINDOW_MIN)
        if prior:
            res.card.flags.append(
                f"중복 가능 — {DUPLICATE_WINDOW_MIN}분 내 같은 번호 접수 {len(prior)}건")
        return db.save_intake(res.card, phone, "전화",
                              status="임시 접수" if res.profile is None else "접수 대기")
    except Exception:
        return None
