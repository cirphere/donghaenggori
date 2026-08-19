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
import datetime
import hashlib
import hmac
import logging
import os
import re
import tempfile
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..core import db, pipeline
from ..core import requesttype as rt_mod
from ..core.korean import josa

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
#
# **빈 값도 '미설정'으로 본다.** .env 는 `KEY=` 로 비워두는 일이 흔한데,
# os.environ.get 은 그때 빈 문자열을 돌려주므로 int("") 가 터진다. 전화가
# 아니라 앱 전체가 못 뜬다.
def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("%s 값이 숫자가 아니라 기본값 %d 를 쓴다: %r", name, default, raw)
        return default


MAX_RECORD_SECONDS = _int_env("CLAWOPS_MAX_RECORD_SECONDS", 60)


# 안내 음성. **비우면 무료 기본 음성**이고, 채우면 글자수 요금이 붙는다
# ("cartesia" 또는 "cartesia:<음성 ID>").
#
# 기본을 비워 둔 것은 요금 때문만이 아니다. 유료 음성 쪽이 실패했을 때 통화가
# 어떻게 되는지 우리가 확인하지 못했다 — 시연 중에 그걸 처음 보고 싶지 않다.
# 평소에는 무료로 돌리고 시연에서만 켠 뒤, 문제가 생기면 변수를 비우고
# 재시작하면 즉시 돌아온다.
#
# 값을 검사하는 이유: 이건 XML 속성에 그대로 들어간다. 따옴표나 공백이 섞이면
# <Say> 태그가 깨져 **통화 전체가 무음이 된다.** 오타 하나로 시연이 날아가는
# 자리라, 모양이 안 맞으면 무료 기본 음성으로 되돌리고 경고를 남긴다.
_VOICE_SHAPE = re.compile(r"^[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?$")


def _voice_env() -> str:
    raw = (os.environ.get("CLAWOPS_VOICE") or "").strip()
    if not raw:
        return ""
    if not _VOICE_SHAPE.match(raw):
        _log.warning("CLAWOPS_VOICE 모양이 맞지 않아 무료 기본 음성을 쓴다: %r", raw)
        return ""
    return raw


VOICE = _voice_env()

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
DUPLICATE_WINDOW_MIN = _int_env("CLAWOPS_DUPLICATE_WINDOW_MIN", 10)

# 통화 앞에서 누른 번호 → 접수에 남길 답변과 상태.
# '확인됨' 은 쓰지 않는다. 버튼을 눌렀다는 것이 본인이라는 증거는 아니다.
_IDENTITY_ANSWER = {
    "self": ("1번(본인 맞다고 응답)", "추정"),
    "other": ("2번(본인이 아니라고 응답) — 성함·주소를 말로 남김", "확인 필요"),
    "unknown": ("응답 없음(키 입력 없이 진행)", "확인 필요"),
    # 묻지 않았다. '응답 없음' 과 구분한다 — 물어봤는데 답이 없는 것과
    # 아예 묻지 않은 것은 나중에 "왜 확인 안 했나" 를 물었을 때 답이 다르다.
    "skipped": ("본인 확인 질문을 하지 않음 — 등록된 발신번호로 조회", "추정"),
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

def _with_done_hint(text: str) -> str:
    """종료 방법 안내가 없으면 붙인다.

    시연장에서 `CLAWOPS_GREETING` 으로 인사말을 갈아끼우는데, 그때 종료 안내가
    통째로 빠지기 쉽다. 무음 종료가 없으므로 그러면 어르신은 말을 마치고도
    상한(1분)까지 침묵 속에 붙들려 있다가 끊어 버린다. 문구를 어떻게 다듬든
    "누르세요"는 남아야 한다.
    """
    return text if "눌러" in text or "누르" in text else f"{text.rstrip()} {DONE_HINT}"


# 인사만 한다. 종료 안내는 붙이지 않는다 — 바로 뒤에 성함 질문이 오고, 녹음
# 직전 안내는 그쪽이 갖는다. 여기에도 붙이면 "누르라"는 말이 두 번 나온다.
GREETING = os.environ.get("CLAWOPS_GREETING") or "동행고리입니다. 처음 연락 주셨네요."

# 성함·읍면동만 따로 받는다. 문의와 한 번에 받으면 이름을 긴 문장에서 골라내야
# 하고, 접수 원문에 신상 이야기가 섞인다.
WHO_PROMPT = _with_done_hint(
    "삐 소리 후 어르신 성함과 사시는 읍면동을 말씀해 주세요.")

# 이 답은 짧다 — "이영희요, 목포시 용당동" 이면 끝이다. 문의 녹음(1분)만큼
# 줄 이유가 없고, 길게 두면 키를 안 누른 어르신이 그만큼 더 기다린다.
IDENTITY_SECONDS = _int_env("CLAWOPS_IDENTITY_SECONDS", 20)

BYE = "담당자가 확인한 뒤 연락드리겠습니다. 감사합니다."

SYMPTOM_PROMPT = ("어디가 편찮으신지, 어느 병원에 언제 가시는지 말씀해 주세요. "
                  + DONE_HINT)
# OTHER_PROMPT 는 없앴다. 성함과 문의를 한 문장으로 몰아 묻던 것인데, 이제
# 성함은 앞 단계(WHO_PROMPT)에서 따로 받고 여기서는 문의만 묻는다.

# 기동할 때 **실제로 적용된 값**을 남긴다.
#
# .env 가 코드 기본값을 조용히 덮어써서 하루에 두 번 헤맸다 — 이름 확인이
# 안 나온 것(DEMO_CALLER_PHONE 미설정)도, 녹음이 30 초에서 잘리는 것도
# 코드만 봐서는 알 수 없다. 소스에 60 이라 적혀 있어도 배포본이 그 값으로
# 도는지는 별개다. 통화 한 번 걸어보기 전에 로그로 확인할 수 있어야 한다.
_log.info("전화 설정 — 녹음 상한 %d초 · 종료키 %s · 시연매핑 %s · 안내음성 %s",
          MAX_RECORD_SECONDS, FINISH_ON_KEY or "(없음)",
          "켬" if (DEMO_CALLER_PHONE and DEMO_CALLER_TARGET) else "끔",
          f"{VOICE} (글자수 과금)" if VOICE else "무료 기본")


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
    # VOICE 는 _voice_env 에서 모양을 검사해 통과한 값이라 속성에 그대로 넣어도 된다
    voice = f' voice="{VOICE}"' if VOICE else ""
    return f'<Say language="ko"{voice}>{_esc(text)}</Say>'


def _record(action: str, seconds: int | None = None) -> str:
    return (f'<Record maxLength="{seconds or MAX_RECORD_SECONDS}" playBeep="true" '
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

        등록된 번호   → "어디가 편찮으신지 …" → 녹음
        미등록 번호   → "성함과 사시는 읍면동을 …" → 녹음 → 증상 녹음

    등록된 번호에는 아무것도 되묻지 않는다. 한때 "박순자 님 맞으신가요?
    1번/2번" 을 먼저 물었는데, 그 답이 접수 카드를 바꾸지 않았다 — 1번을
    눌러도 안 눌러도 대상자는 '확인됨' 이고 확정 게이트도 똑같이 열렸다.
    통화만 한 턴 길어졌다.

    미등록 번호는 다르다. 이름을 모르면 복지사에게 "누구인지 모르는 접수" 가
    남고 발신번호로 되걸어 보는 수밖에 없어서, 성함·읍면동을 먼저 받는다.
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
        return _xml(_say(GREETING + _recording_notice())
                    + _ask_identity_first(request, "new"))

    # 등록된 번호 — 바로 증상을 받는다.
    #
    # 예전에는 여기서 "박순자 님 맞으신가요? 1번/2번" 을 먼저 물었다. 뺀 이유는
    # **그 답이 접수 카드를 바꾸지 않았기 때문**이다. 1번을 눌러도, 아무것도
    # 누르지 않아도 대상자는 똑같이 '확인됨' 이고 확정 게이트도 똑같이 열린다
    # (등록된 발신번호로 이미 정해지기 때문이다). 통화만 한 턴 길어졌다.
    #
    # **2번(본인 아님)은 카드를 바꿨다.** 그 경로만 대상자를 '확인 필요' 로
    # 내리고 성함·읍면동을 따로 받았다. 그걸 잃는다 — 어르신 전화기로 다른
    # 사람이 걸면 이제 그 어르신 본인의 요청으로 기록된다. 화면에서 대상자를
    # 고쳐야 하고, 그건 사회복지사가 통화 원문을 읽고 판단할 몫이다.
    # 등록된 어르신은 GREETING 을 듣지 않는다. 녹음 고지는 여기에도 붙여야
    # **모든 발신자가** 듣는다 — 재이용자가 오히려 녹음이 많이 쌓이는 쪽이다.
    return _xml(_say(_recording_notice().strip() + " " + SYMPTOM_PROMPT
                     if _recording_notice() else SYMPTOM_PROMPT)
                + ask("skipped"))


@router.post("/identity", name="voice_identity")
async def identity(request: Request) -> Response:
    """1번(본인) / 2번(아니오) 응답. 어느 쪽이든 다음은 녹음이다."""
    form = await _verify(request)
    digit = (form.get("Digits") or "").strip()
    who = "self" if digit == "1" else "other" if digit == "2" else "unknown"
    if who == "other":
        # 번호 주인이 아니라고 했다 — 누구인지부터 따로 받는다.
        return _xml(_say(WHO_PROMPT) + _ask_identity_first(request, "other", say=False))
    action = _callback(request, "voice_recording") + f"?who={who}"
    return _xml(_say(SYMPTOM_PROMPT) + _record(action))


def _ask_identity_first(request: Request, who: str, say: bool = True) -> str:
    """성함·읍면동을 **문의 내용과 따로** 받는다.

    한 번에 받으면 두 가지가 나빠진다. 이름을 긴 문장에서 규칙으로 골라내야
    해서 정확도가 떨어지고("저는 이영희고요 목포시 용당동 사는데 무릎이…"),
    접수 카드의 원문에 신상 이야기가 섞여 복지사가 문의 내용을 찾아 읽어야
    한다. 따로 받으면 짧은 전용 답변에서 뽑고, 원문에는 문의만 남는다.

    이 녹음은 짧다. 상한을 문의 녹음(1분)만큼 줄 이유가 없다.
    """
    action = _callback(request, "voice_identity_record") + f"?who={who}"
    return (_say(WHO_PROMPT) if say else "") + _record(action, IDENTITY_SECONDS)


@router.post("/identity-record", name="voice_identity_record")
async def identity_record(request: Request) -> Response:
    """성함·읍면동 녹음이 끝났다. 곧바로 문의 내용을 묻는다.

    **여기서 전사하지 않는다.** 예전에는 이 자리에서 STT 를 돌렸는데, 그동안
    통화가 무음이 되고 그다음 안내 멘트가 이어졌다. 무음·멘트 중에 누른 키는
    전부 버려진다 — finishOnKey 는 삐 소리 후 녹음이 시작된 뒤에만 듣는다.
    그래서 "첫 질문에선 키가 먹는데 다음 질문에선 안 먹는" 증상이 났다:
    첫 질문은 즉시 삐가 나오지만, 두 번째는 STT 대기만큼 늦게 나왔다.

    녹음 URL 만 보관하고 전사는 통화 맨 끝(/recording)으로 미룬다. 거기는
    이미 문의 녹음 STT 를 기다리는 자리라, 짧은 성함 녹음 하나가 늘어도
    접수 안내가 조금 늦어질 뿐이고 — 접수는 이미 그 안에서 저장되므로
    잘려도 잃는 것이 없다.

    **여기서 실패해도 통화를 끊지 않는다.** 이름을 못 받는 것보다 문의를 통째로
    놓치는 쪽이 훨씬 나쁘다.
    """
    form = await _verify(request)
    who = (request.query_params.get("who") or "new").strip()
    call_id = (form.get("CallId") or "").strip()

    url = (form.get("RecordingUrl") or "").strip()
    try:
        duration = float(form.get("RecordingDuration") or 0)
    except ValueError:
        duration = 0
    if url and duration > 0 and call_id:
        _remember_identity(call_id, url)
    else:
        _log.info("성함 녹음 없음 — duration=%s url=%s",
                  form.get("RecordingDuration"), "있음" if url else "없음")

    action = _callback(request, "voice_recording") + f"?who={who}"
    return _xml(_say(SYMPTOM_PROMPT) + _record(action))


# 통화 한 건 안에서만 쓰는 임시 보관 — CallId → 성함 녹음의 서명 URL.
#
# 신원 녹음과 문의 녹음이 **다른 웹훅**으로 들어와서, 앞 단계 결과를 뒤로 넘길
# 자리가 필요하다. DB 에 넣지 않는 이유는 접수로 이어지지 못한 통화(중간에
# 끊김)의 신상 발화가 남지 않게 하기 위해서다. 전사를 통화 끝으로 미루면서
# 텍스트 대신 URL 을 담게 됐다 — 신상 발화 원문이 메모리에 남지 않는 부수
# 효과도 있다(테스트는 여전히 텍스트를 넣는데, 꺼내 쓰는 쪽이 값을 해석하지
# 않으므로 상관없다).
_IDENTITY_SAID: dict[str, tuple[float, str]] = {}
_IDENTITY_TTL = 600      # 통화 하나가 이보다 길 이유가 없다


def _remember_identity(call_id: str, text: str) -> None:
    now = time.monotonic()
    # 끊긴 통화가 쌓이지 않게 지날 때마다 오래된 것을 턴다.
    for k in [k for k, (t, _) in _IDENTITY_SAID.items() if now - t > _IDENTITY_TTL]:
        _IDENTITY_SAID.pop(k, None)
    _IDENTITY_SAID[call_id] = (now, text)


def _take_identity(call_id: str) -> str | None:
    """한 번 꺼내면 지운다 — 접수에 실었으면 더 들고 있을 이유가 없다."""
    got = _IDENTITY_SAID.pop(call_id, None)
    return got[1] if got else None


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

    who = (request.query_params.get("who") or "unknown").strip()

    # 2번을 눌렀다 — 번호 주인이 아니라고 **직접 밝혔다.** 그 말을 따른다.
    # 그대로 두면 필요도(장기요양등급)와 병원 추천이 번호 주인 것으로 붙는데,
    # 카드에 '확인 필요' 가 떠도 내용 자체가 남의 정보라 복지사가 그 표시를
    # 놓치면 엉뚱한 기준으로 동행을 준비하게 된다.
    # 앞 단계에서 받아 둔 성함 녹음을 이제야 전사한다(identity_record 주석
    # 참고 — 통화 중간의 무음을 없애려고 미뤘다). 문의 원문과 **섞지 않는다**
    # — 접수 카드의 원문은 문의 내용만 담아야 복지사가 찾아 읽지 않는다.
    said_who = None
    said_url = _take_identity((form.get("CallId") or "").strip())
    if said_url:
        try:
            said_who = _transcribe_url(said_url).strip() or None
        except Exception as e:               # 이름을 못 얻어도 접수는 진행한다
            _log.warning("성함 녹음 처리 실패 — %s: %s", type(e).__name__, e)

    res = pipeline.run(phone, text, channel="전화",
                       identity_denied=(who == "other"), identity_utterance=said_who)
    if res.urgent:
        return _transfer(request, "긴급한 상황으로 보입니다.", _save(res, phone, text))

    intake_id = _save(res, phone, text)

    # 통화 앞에서 받은 1번/2번 응답을 접수에 남긴다. 눌렀다는 사실이 근거일 뿐
    # 본인확인이 아니다 — 남의 폰으로 건 사람도 1번을 누를 수 있다. 최대 '추정'
    # 이고 확정은 사회복지사가 한다.
    if intake_id:
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

    # **키가 실제로 전달됐는지 여기서만 알 수 있다.**
    #
    # "다 말하고 아무 버튼 눌러도 안 끊긴다" 는 보고가 들어왔는데, 원인이
    # 두 가지라 로그 없이는 못 가른다.
    #
    #   ① DTMF 가 회선에서 안 온다 → 녹음이 maxLength 까지 간다
    #   ② 키는 먹었는데 그 뒤 전사 대기가 길다 → 누른 사람은 똑같이 느낀다
    #
    # Digits 에 누른 키가 담겨 오고(문서), 상한까지 갔는지는 duration 으로
    # 안다. 둘을 같이 찍으면 다음 통화 한 번으로 판명된다. ①이면 상한을
    # 낮추는 것 말고 할 수 있는 게 없고(무음 종료 미지원), ②면 안내 문구로
    # 푼다 — 처방이 아예 다르다.
    digits = (form.get("Digits") or "").strip()
    _log.info("녹음 종료 — 누른 키 %s · 길이 %.1f초 / 상한 %d초%s",
              repr(digits) if digits else "없음", duration, MAX_RECORD_SECONDS,
              " (상한까지 감 — 키가 안 먹었을 수 있다)"
              if duration >= MAX_RECORD_SECONDS - 1 else "")

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
        _keep_sample(audio, text)
        return text
    finally:
        os.unlink(tmp.name)


# ─────────────────────────────────────────── 학습 표본 보관 --

# 통화 녹음을 남길지. **기본은 끔.**
#
# 남기지 않는 것이 원래 설계다 — 접수로 이어지지 못한 통화의 신상 발화가
# 디스크에 쌓이지 않게 하려는 것이었다. 그 판단은 지금도 옳다.
#
# 그런데 그 때문에 **STT 를 개선할 방법이 없다.** 무엇을 틀리는지 재려면
# (음성, 정답 텍스트) 쌍이 필요한데, 전사 직후 지우면 하루를 써도 한 건도
# 안 남는다. 모델을 바꿔도 좋아졌는지 잴 수가 없다.
#
# 그래서 **켤 수 있게** 두되 기본은 끈 채로 둔다. 켜는 것은 운영 판단이고,
# 켤 때는 안내 멘트에 녹음 보관을 알리는 것이 전제다.
KEEP_SAMPLES = (os.environ.get("VOICE_KEEP_SAMPLES") or "").strip().lower() in (
    "1", "true", "yes")
SAMPLE_DIR = os.environ.get("VOICE_SAMPLE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "voice_samples")

# 보관 기간(일). **무기한으로 쌓지 않는다.** 어르신의 목소리와 건강 이야기라
# "언젠가 쓸지 모른다" 로 남겨 둘 성질이 아니다. 0 이하면 안 지운다 —
# 기관이 별도 보관 규정을 갖고 스스로 관리할 때만 그렇게 둔다.
SAMPLE_RETENTION_DAYS = _int_env("VOICE_SAMPLE_RETENTION_DAYS", 30)

# 녹음 보관을 켠 채로 알리지 않는 것은 안 된다(README 가 켜기 전 전제로 적어
# 둔 것이기도 하다). **KEEP_SAMPLES 를 끄면 이 문장도 같이 사라진다** —
# 안내와 실제 동작이 어긋나지 않게 한 곳에 묶어 둔다.
RECORDING_NOTICE = os.environ.get("CLAWOPS_RECORDING_NOTICE") or (
    "통화 내용은 상담 품질을 높이기 위해 녹음되어 보관됩니다.")


def _recording_notice() -> str:
    """첫 안내에 덧붙일 녹음 고지. 보관을 끄면 빈 문자열이다."""
    return f" {RECORDING_NOTICE}" if KEEP_SAMPLES else ""


def _prune_samples() -> None:
    """보관 기간이 지난 표본을 지운다.

    지우는 시점을 따로 두지 않고 **새로 쌓을 때 함께** 판다. 크론이나 별도
    프로세스를 두면 그것이 안 도는 환경에서 조용히 무기한 보관이 된다.
    """
    if SAMPLE_RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - SAMPLE_RETENTION_DAYS * 86400
    removed = 0
    for name in os.listdir(SAMPLE_DIR):
        if not name.endswith((".wav", ".txt")):
            continue                          # README.md 는 건드리지 않는다
        path = os.path.join(SAMPLE_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed:
        _log.info("보관 기간(%d일) 지난 표본 %d개 삭제", SAMPLE_RETENTION_DAYS, removed)


def _keep_sample(audio: bytes, text: str) -> None:
    """음성과 전사를 짝지어 남긴다 — 나중에 사람이 전사를 고치면 그것이 정답이다.

    **STT 출력을 정답으로 쓰지 않는다.** 여기 남는 .txt 는 초안이고, 사람이
    들으면서 고쳐야 학습에 쓸 수 있는 라벨이 된다. 그 구분을 파일 이름이 아니라
    같은 폴더의 README 로 남긴다.

    실패해도 통화를 막지 않는다. 보관은 부가 기능이고 접수가 본체다.
    """
    if not KEEP_SAMPLES:
        return
    try:
        os.makedirs(SAMPLE_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        with open(os.path.join(SAMPLE_DIR, f"{stamp}.wav"), "wb") as f:
            f.write(audio)
        with open(os.path.join(SAMPLE_DIR, f"{stamp}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        _log.info("학습 표본 보관 — %s (%d바이트)", stamp, len(audio))
        _prune_samples()
    except Exception as e:                   # 디스크가 차도 통화는 계속된다
        _log.warning("표본 보관 실패 — %s: %s", type(e).__name__, e)


def _receipt(res) -> str:
    c = res.card
    if c is None:
        return "접수했습니다."
    # 기존 흐름이 다루지 않는 요청(새 병원 탐색·진료과 탐색·인력 요청)은 날짜를
    # 확정해서 들려주지 않는다. 화면에는 '확인 필요' 배지가 뜨지만 **통화에는
    # 그런 장치가 없어서**, "말씀하신 날짜로 접수했습니다" 가 어르신에게는 일정이
    # 잡혔다는 말로 들린다. 실제로는 사람이 다시 걸어야 하는 건이다.
    if getattr(c, "request_type", None) in rt_mod.STAFF_HANDLED:
        return ("말씀하신 내용을 접수했습니다. "
                "담당 사회복지사가 확인한 뒤 다시 연락드리겠습니다.")
    when = c.date_label or "말씀하신 날짜"
    if c.hospital and c.hospital_status == "확인됨":
        # 조사를 붙박이로 두면 "행복정형외과으로" 가 된다. ~내과·~치과·~안과 처럼
        # 받침 없이 끝나는 의원 이름이 흔한데, 이게 어르신이 통화에서 마지막으로
        # 듣는 문장이다. korean.josa 가 받침을 보고 '으로/로' 를 고른다.
        return f"{when} {josa(c.hospital, '로')} 접수했습니다."
    return f"{josa(when, '로')} 접수했습니다."


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
