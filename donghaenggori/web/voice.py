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
# 녹음 상한(초). 2턴이 되면서 통화가 길어졌다 — 인사·질문·대기까지 합치면
# 어르신이 붙들려 있는 시간이 금세 1분을 넘는다. 짧게 잡는다.
MAX_RECORD_SECONDS = int(os.environ.get("CLAWOPS_MAX_RECORD_SECONDS", "30"))
# 같은 번호의 재전화를 중복 후보로 표시할 시간 범위(분)
DUPLICATE_WINDOW_MIN = int(os.environ.get("CLAWOPS_DUPLICATE_WINDOW_MIN", "10"))

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
# 않는다. 안내가 없으면 어르신은 30초를 기다리거나 끊어 버린다.
#
# 끊으면 2턴(본인 확인)을 못 하므로 우물정자를 안내한다. 급한 경우의 119 안내는
# STT 를 기다리지 않는 유일한 경로라 짧게라도 남긴다.
# 시연장에서 문구를 다듬을 수 있도록 환경변수로 뺐다.
# 길이는 더 줄이기 어렵다 — 안내를 넣으면 그만큼 늘어난다. 대신 **순서**를
# 바꿨다. 끝까지 안 듣고 말을 시작하거나 끊는 사람이 있으므로, 당장 해야 할
# 일을 앞에 두고 119 안내를 뒤로 뺐다.
GREETING = os.environ.get("CLAWOPS_GREETING") or (
    "동행고리입니다. 삐 소리 후 병원과 날짜를 말씀하시고, "
    "마치면 우물 정자를 눌러 주세요. "
    "급하시면 끊고 119로 전화하세요.")

BYE = "담당자가 확인한 뒤 연락드리겠습니다. 감사합니다."


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
            f'finishOnKey="#" action="{_esc(action)}"/>')


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

    joined = "".join(f"{k}{v}" for k, v in sorted(form.items()))
    tried: list[str] = []
    for url in _url_candidates(request):
        for label, data in (("url+params", url + joined), ("url", url)):
            tried.append(f"{label}:{url}")
            if hmac.compare_digest(got, _sign(data)):
                _log.info("서명 확인 — 기준: %s %s", label, url)
                return form
    # 본문 그대로, 파라미터만 — 다른 관례도 한 번씩 시도한다
    for label, data in (("body", raw.decode(errors="replace")), ("params", joined)):
        tried.append(label)
        if hmac.compare_digest(got, _sign(data)):
            _log.info("서명 확인 — 기준: %s", label)
            return form

    # 이 로그가 유일한 단서다. 보내는 쪽이 어떤 URL 로 서명했는지 알 수 없으니,
    # 우리가 복원한 URL 들을 그대로 남겨 대조할 수 있게 한다. 키는 넣지 않는다.
    _log.warning(
        "서명 불일치\n  받은 서명   : %s…\n  파라미터 키 : %s\n"
        "  시도한 URL  :\n%s",
        got[:12], sorted(form),
        "\n".join(f"    - {u}" for u in _url_candidates(request)))
    raise HTTPException(401, "서명이 올바르지 않습니다")


# ──────────────────────────────────────────────────── 1턴 수신 --

@router.post("/incoming")
async def incoming(request: Request) -> Response:
    """전화가 걸려왔다. 인사하고 녹음을 시작한다. 대화는 하지 않는다."""
    await _verify(request)
    return _xml(_say(GREETING) + _record(_callback(request, "voice_recording")))


def _lookup_phone(raw: str) -> str:
    """조회에 쓸 번호. 시연용 매핑이 걸려 있으면 바꿔 끼운다."""
    if DEMO_CALLER_PHONE and DEMO_CALLER_TARGET:
        if db.normalize_phone(raw) == db.normalize_phone(DEMO_CALLER_PHONE):
            return DEMO_CALLER_TARGET
    return raw


@router.post("/recording", name="voice_recording")
async def recording(request: Request) -> Response:
    """요청 내용 녹음이 끝났다. 어르신은 아직 통화 중이다."""
    form = await _verify(request)
    phone = _lookup_phone(form.get("From") or "")

    text = _read_recording(form)
    if text is None:
        return _hangup("말씀이 녹음되지 않았습니다. 다시 걸어주시거나 담당자에게 연락 주세요.")
    if not text:
        return _hangup("말씀을 알아듣지 못했습니다. 담당자가 확인 후 연락드리겠습니다.")

    res = pipeline.run(phone, text, channel="전화")
    if res.urgent:
        return _transfer(request, "긴급한 상황으로 보입니다.", _save(res, phone, text))

    intake_id = _save(res, phone, text)
    question = _identity_question(res)
    if not question or intake_id is None:
        return _hangup(f"{_receipt(res)} {BYE}")

    # 2턴 — 누구인지 되묻는다. 확인은 하지 않고 답만 받아둔다.
    action = f'{_callback(request, "voice_confirm")}?intake={intake_id}'
    return _xml(_say(question + " 말씀하신 뒤 우물 정자를 눌러 주세요.") + _record(action))


# ──────────────────────────────────────────────────── 2턴 확인 --

@router.post("/confirm", name="voice_confirm")
async def confirm(request: Request) -> Response:
    """본인 확인 답변이 녹음됐다. 해석하지 않고 원문을 남긴다."""
    form = await _verify(request)
    phone = _lookup_phone(form.get("From") or "")
    try:
        intake_id = int(request.query_params.get("intake") or 0)
    except ValueError:
        intake_id = 0

    text = _read_recording(form)
    if not text:
        # 답변 없이 끊었거나 못 알아들었다. 접수는 이미 저장돼 있다.
        if intake_id:
            db.attach_identity_answer(intake_id, "", "확인 필요")
        return _hangup("담당자가 확인 후 연락드리겠습니다. 감사합니다.")

    # 확인 답변에서도 긴급이 나올 수 있다 — "아이고 숨이 차" 같은 말
    c = pipeline._classify(text, use_llm=None)
    if c.analysis.urgent:
        if intake_id:
            db.attach_identity_answer(intake_id, text, "확인 필요")
        return _transfer(request, "긴급한 상황으로 보입니다.", intake_id or None)

    status = _match_status(phone, text)
    if intake_id:
        db.attach_identity_answer(intake_id, text, status)
    return _hangup(f"말씀 감사합니다. {BYE}")


# ─────────────────────────────────────────────────────── 도우미 --

def _identity_question(res) -> str | None:
    """누구인지 되물을 문장. 물어볼 게 없으면 None."""
    c = res.card
    if c is None:
        return None
    cands = c.target_candidates or []
    rel = c.proxy_relation or "어르신"
    if len(cands) == 1:
        return (f"{rel}이신 {cands[0]['name']} 님 맞으실까요? "
                "맞으시면 성함을 한 번 더 말씀해 주세요.")
    if len(cands) > 1:
        return "어느 어르신이신지 성함을 말씀해 주세요."
    if res.profile:
        return (f"{res.profile['name']} 님 맞으실까요? "
                "맞으시면 성함을 한 번 더 말씀해 주세요.")
    return "어르신 성함과 사시는 읍면동을 말씀해 주세요."


def _match_status(phone: str, answer: str) -> str:
    """답변에 후보 이름이 들어 있는지 **문자열 대조만** 한다.

    답변을 해석해 사람을 확정하지 않는다. 이름이 나왔다는 사실만 근거로 삼고,
    안 나왔으면 오히려 확인이 필요하다고 낮춘다. 어느 쪽이든 확정은 사람이 한다.
    """
    names = []
    prof = db.get_profile(phone)
    if prof:
        names.append(prof["name"])
    names += [c["name"] for c in db.find_by_guardian_phone(phone)]
    if not names:
        return "확인 필요"          # 미등록 — 들은 이름은 원문으로만 남는다
    flat = answer.replace(" ", "")
    return "추정" if any(n.replace(" ", "") in flat for n in names) else "확인 필요"


def _read_recording(form: dict) -> str | None:
    """녹음을 내려받아 전사한다. None=녹음 없음, ""=전사 실패."""
    url = form.get("RecordingUrl") or ""
    try:
        duration = float(form.get("RecordingDuration") or 0)
    except ValueError:
        duration = 0
    if not url or duration <= 0:
        return None
    try:
        return _transcribe_url(url).strip()
    except Exception:
        return ""


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
