"""동행고리 AI — REST API (FastAPI).

프론트엔드가 소비할 JSON API가 본체이고, 개발 확인용 HTML은 최소한만 둔다.
화면 5종(홈·입력·접수카드·실패대응·사후기록)에 필요한 엔드포인트를 모두 제공한다.

실행:
    uvicorn donghaenggori.web.api:app --reload --port 8000
문서:
    http://localhost:8000/docs   (Swagger — 프론트 담당자와 이걸로 협의)
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Literal

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from ..core import db, gate, pipeline
from ..services import rag, stt, summarize
from . import voice

app = FastAPI(
    title="동행고리 AI",
    description="사회복지사를 위한 병원동행 접수·이력정리 Copilot — AI는 후보·근거만, 확정은 사람",
    version="0.1.0",
)

# 프론트는 별도 오리진에서 뜬다(localhost:3000 등). CORS가 없으면 브라우저가
# 막아서 연동 자체가 시작되지 않는다. 기본은 전부 허용 — 이 API는 어차피
# 인증이 없어 CORS로 지켜지는 것이 없고, 좁혀봐야 프론트만 막힌다.
# 운영에서 제한하려면 .env 의 CORS_ORIGINS 에 도메인을 콤마로 나열한다.
_origins = [o.strip() for o in (os.environ.get("CORS_ORIGINS") or "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,      # "*" 와 함께 쓰면 브라우저가 거부한다
    allow_methods=["*"],
    allow_headers=["*"],
)


# 전화(ClawOps VoiceML) — 키가 없으면 라우트는 뜨되 요청을 거절한다
app.include_router(voice.router)


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    _log_ai_placement()
    _schedule_warmup()


def _schedule_warmup() -> None:
    """WARMUP_ON_START 가 켜져 있으면 기동 직후 예열을 돌린다.

    예열은 모델 로드와 외부 API 캐시 채우기라 30초 넘게 걸린다. 사람이
    `curl -X POST /api/warmup` 을 잊으면, 시연 첫 요청이 그 시간을 대신 문다.

    **기동을 막지 않는다.** 별도 스레드로 돌려서 서버는 곧바로 요청을 받고
    헬스체크도 통과한다. 예열 전에 들어온 요청은 느릴 뿐 실패하지 않는다.

    도커에서만 켠다(docker-compose.yml). 로컬 실행과 테스트에서 매번 모델을
    받으면 곤란해서 코드 기본값은 꺼둔다.
    """
    import asyncio
    import logging
    import os
    import time

    if (os.environ.get("WARMUP_ON_START") or "").strip().lower() not in ("1", "true", "yes"):
        return
    log = logging.getLogger("uvicorn.error")

    async def _run() -> None:
        t0 = time.monotonic()
        log.info("예열 시작 — 모델 로드와 외부 API 캐시 (수십 초, 요청은 그동안에도 받는다)")
        try:
            # 엔드포인트(warmup)가 아니라 본체(_warmup)를 부른다 — 파이썬
            # 호출에는 FastAPI 의존성 주입이 돌지 않는다.
            res = await asyncio.to_thread(_warmup)
        except Exception as e:                     # 예열 실패가 서비스를 막지 않는다
            log.warning("예열 실패 — %s: %s", type(e).__name__, e)
            return
        bad = {k: v for k, v in (res.get("warmed") or {}).items()
               if isinstance(v, str) and v not in ("ok", "loaded", "BERT", "TF-IDF")}
        if bad:
            log.warning("예열 완료 (%.1f초) — 확인 필요: %s", time.monotonic() - t0, bad)
        else:
            log.info("예열 완료 (%.1f초) — 전부 정상", time.monotonic() - t0)

    asyncio.get_running_loop().create_task(_run())


def _log_ai_placement() -> None:
    """AI 가 어디서 도는지 기동 로그에 남긴다.

    "어르신 발화가 외부로 나가지 않는다" 는 심사에서 내세우는 논리인데, 이건
    코드가 아니라 **.env 상태**에 달려 있다. ANTHROPIC_API_KEY 를 한 줄 채우면
    조용히 켜진다(nlu.analyze 는 키가 있으면 use_llm 을 True 로 본다) — 팀원이
    선의로 키를 넣어도 아무 표시가 없다.

    시연 직전에 로그 한 줄로 확인할 수 있어야 한다.
    """
    import logging
    import os

    log = logging.getLogger("uvicorn.error")
    stt = os.environ.get("WHISPER_DEVICE", "cpu")
    log.info("AI 배치 — 음성인식 %s(%s) · 긴급분류 자체 모델 · 외부 LLM %s",
             os.environ.get("WHISPER_MODEL", "small"), stt,
             "사용(발화가 외부로 나감)" if os.environ.get("ANTHROPIC_API_KEY")
             else "미사용")


# ------------------------------------------------------------ 스키마 --

class Policy(BaseModel):
    """이 서비스가 응답마다 지키는 약속.

    값이 리터럴로 고정돼 있어서, 누가 나중에 "AI가 응급 여부를 판정한다" 쪽으로
    코드를 바꾸면 응답 검증에서 터진다. 원칙을 주석이 아니라 계약으로 만든 것이다.
    """
    medical_judgement: Literal[False] = False       # AI는 의료 판단을 하지 않는다
    human_review_required: Literal[True] = True     # 모든 결과는 사람 검토가 전제다
    ai_scope: Literal["후보·근거 제시까지"] = "후보·근거 제시까지"


class FieldView(BaseModel):
    """접수카드 항목 하나. 확률(%)은 두지 않는다 — 상태 3단계와 근거 문장으로만 말한다."""
    label: str
    value: str | None = None
    status: Literal["확인됨", "추정", "확인 필요"]
    evidence: list[str] = []
    spoken: str | None = None                       # 어르신이 실제로 말한 표현


class CardOut(BaseModel):
    # 카드의 나머지 키(need_level·flags·outing_checklist…)는 그대로 통과시킨다.
    # 여기 안 적었다고 응답에서 빠지면 프론트가 조용히 깨진다.
    model_config = ConfigDict(extra="allow")
    target: str
    hospital: str | None = None
    hospital_status: Literal["확인됨", "추정", "확인 필요"]
    fields: dict[str, FieldView]
    confirm_questions: list[str] = []


class IntakeOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    urgent: bool
    urgent_confident: bool = True
    urgent_message: str | None = None
    card: CardOut | None = None                     # 긴급이면 카드를 만들지 않는다
    policy: Policy = Policy()


class IntakeIn(BaseModel):
    phone: str = Field(..., description="발신번호 — 보조 식별 단서일 뿐, 대상자 확정 아님")
    utterance: str = Field(..., description="발화 텍스트 (STT 결과 또는 직접 입력)")
    channel: str = Field("전화", description="전화 | 앱·웹(보호자) | 직접(기관)")
    save: bool = Field(True, description="접수 기록으로 저장할지")


class GuardianIntakeIn(BaseModel):
    """보호자 웹 전용 — channel·save는 클라이언트가 정하지 못한다(서버가 고정)."""
    phone: str = Field(..., description="발신번호 — 보조 식별 단서일 뿐, 대상자 확정 아님")
    utterance: str = Field(..., description="신청 내용")


class GuardianIntakeOut(BaseModel):
    """보호자 웹 응답 — **부른 사람이 스스로 넣은 것만 돌려준다.**

    이 엔드포인트는 로그인 없이 열려 있고, phone 은 누구나 임의로 적을 수 있다.
    저장된 기록에서 나온 값을 하나라도 돌려주면 그 순간 조회 API 가 된다 —
    번호를 바꿔가며 부르면 등록된 어르신의 이름·보호자 연락처·거동 상태·독거
    여부·진료 이력이 그대로 빠져나간다(실제로 재현했다).

    그래서 직원용 IntakeOut 을 쓰지 않는다. 여기 있는 값은 전부 발신자가 적어
    보낸 문장에서 나온 것이라, 돌려줘도 새로 알려주는 것이 없다.

    **절대 넣지 않는 것** — profile, card, target, target_candidates,
    facilities, 그리고 과거 이력에서 고른 병원. card 는 근거 문장에도 이력이
    들어간다("최근 6개월 내 ○○정형외과의원 2회 방문").
    """
    ok: bool = True
    intake_id: int | None = None
    urgent: bool = False
    urgent_confident: bool = True
    urgent_message: str | None = None
    raw_utterance: str = ""                          # 본인이 적어 보낸 문장
    dept: str | None = None                          # 그 문장에서 뽑은 진료과
    date: dict | None = None                         # 그 문장에서 뽑은 날짜
    policy: Policy = Policy()


class ConfirmIn(BaseModel):
    hospital: str
    date: str
    level: str
    acknowledge: bool = Field(
        False, description="확인 필요가 남은 것을 알고도 확정 — 감사 로그에 남는다")


class VerifyIn(BaseModel):
    field: Literal["target", "hospital", "dept", "date", "time"] = Field(
        ..., description="확인한 항목")
    value: str = Field(..., min_length=1, description="통화로 확인한 값")


class ResolveIn(BaseModel):
    note: str = Field("", description="어떻게 처리했는지 — 감사 로그에 남는다")


class PostRecordIn(BaseModel):
    intake_id: int
    phone: str
    memo: str = Field(..., description="동행 매니저 음성 메모(텍스트)")
    dept: str | None = None
    target: str | None = None


class ApproveIn(BaseModel):
    approved: bool = True


class LoginIn(BaseModel):
    email: str
    password: str


class LoginOut(BaseModel):
    token: str
    user: dict


def current_user(authorization: str | None = Header(None)) -> dict:
    """Authorization: Bearer <token> → 인증된 사용자. 없거나 무효면 401.

    role·actor는 더 이상 요청 본문에서 안 받는다 — 여기서 확인된 신원만 믿는다.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "로그인이 필요합니다 (Authorization: Bearer <token>)")
    token = authorization.split(" ", 1)[1].strip()
    user = db.resolve_session(token)
    if not user:
        raise HTTPException(401, "세션이 만료되었거나 유효하지 않습니다")
    return user


# ------------------------------------------------------------ 인증 --

# 로그인 시도 제한 — 계정 하나당 잠깐 잠근다.
#
# 두 가지를 막는다.
#   1) 비밀번호 무차별 대입. 계정 수가 적고 최소 8자라 제한이 없으면 뚫린다.
#   2) **서비스 정지.** 이게 더 급하다 — 모든 엔드포인트가 동기 def 라
#      Starlette 스레드풀(기본 40)을 공유하는데, 로그인은 무인증에 PBKDF2 를
#      26만 회 돈다. 익명 요청 수십 개면 CPU 와 스레드풀이 동시에 포화되고
#      /api/health 부터 전사까지 전부 멈춘다. 시연 중에 이걸 당하면 끝이다.
#
# 프로세스 메모리에 둔다. 워커가 하나라 충분하고(uvicorn --workers 1),
# 재시작하면 초기화되는 편이 시연에서는 오히려 안전하다.
# 잠금은 짧게(60초) 잡았다. 막는 목적이 CPU 포화라, **잠기기 시작하는 것**이
# 중요하지 오래 잠그는 것이 중요하지 않다 — 잠긴 요청은 PBKDF2 를 돌지 않는다.
# 길게 잡으면 시연 중 오타 몇 번에 발표자가 몇 분을 못 들어간다.
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW = 300.0        # 5분 안에 5회 실패하면
_LOGIN_LOCK = 60.0           # 60초 잠근다


def _login_locked(key: str) -> float:
    """남은 잠금 시간(초). 0 이면 안 잠겼다."""
    import time
    now = time.monotonic()
    fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILS[key] = fails
    if len(fails) < _LOGIN_MAX_FAILS:
        return 0.0
    return max(0.0, _LOGIN_LOCK - (now - fails[-1]))


def _login_failed(key: str) -> None:
    import time
    _LOGIN_FAILS.setdefault(key, []).append(time.monotonic())
    # 안 쓰는 키가 계속 쌓이지 않게. 실서비스 규모가 아니라 이걸로 충분하다.
    if len(_LOGIN_FAILS) > 1000:
        now = time.monotonic()
        for k in [k for k, v in _LOGIN_FAILS.items()
                  if not v or now - v[-1] > _LOGIN_WINDOW]:
            _LOGIN_FAILS.pop(k, None)


@app.post("/api/auth/login", tags=["인증"], response_model=LoginOut)
def login(body: LoginIn) -> dict:
    key = (body.email or "").strip().lower()
    # **비밀번호를 확인하기 전에** 막는다. 확인한 뒤에 막으면 PBKDF2 비용을
    # 이미 치른 뒤라 서비스 정지를 못 막는다.
    left = _login_locked(key)
    if left:
        raise HTTPException(429, f"로그인 시도가 너무 많습니다. {int(left) + 1}초 후 다시 시도해 주세요")

    user = db.verify_login(body.email, body.password)
    if not user:
        _login_failed(key)
        raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다")

    _LOGIN_FAILS.pop(key, None)      # 성공하면 카운터를 지운다
    token = db.create_session(user["id"], settings.session_ttl_seconds)
    return {"token": token, "user": user}


@app.post("/api/auth/logout", tags=["인증"])
def logout(authorization: str | None = Header(None)) -> dict:
    if authorization and authorization.lower().startswith("bearer "):
        db.delete_session(authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@app.get("/api/auth/me", tags=["인증"])
def me(user: dict = Depends(current_user)) -> dict:
    return user


# ------------------------------------------------------------ 상태 --

@app.get("/api/health", tags=["시스템"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status", tags=["시스템"])
def status(user: dict = Depends(current_user)) -> dict:
    """외부 연동 상태 — 키 값은 노출하지 않고 존재 여부만. 운영 정보라 로그인 필요."""
    from ..services import intent_model, stt
    out = {
        "keys": settings.status(),
        "intent_model": ("BERT" if intent_model.bert_available()
                         else "TF-IDF" if intent_model.available() else "미학습(규칙 폴백)"),
        "intent_model_loaded": intent_model.available() or intent_model.bert_available(),
        "stt": {"model": stt.MODEL_SIZE, "device": stt.DEVICE,
                "compute_type": stt.COMPUTE_TYPE},
        "facilities": db.facility_counts(),
    }
    # BERT로 못 올라갔으면 이유를 함께 준다 — 조용한 폴백을 눈에 보이게
    err = intent_model.bert_error()
    if err:
        out["intent_model_fallback_reason"] = err
    return out


# ------------------------------------------- 화면 01 홈 대시보드 --

@app.get("/api/dashboard", tags=["화면01 홈"])
def dashboard(user: dict = Depends(current_user)) -> dict:
    if not db.can(user["role"], "intake.view"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 조회 권한이 없습니다")
    return {"counts": db.intake_counts(), "intakes": db.list_intakes(limit=50)}


@app.get("/api/intakes", tags=["화면01 홈"])
def list_intakes(limit: int = Query(50, le=200), user: dict = Depends(current_user)) -> list[dict]:
    if not db.can(user["role"], "intake.view"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 조회 권한이 없습니다")
    return db.list_intakes(limit=limit)


# ------------------------------------ 화면 02 접수 → 화면 03 카드 --

def _run_intake(body: IntakeIn) -> dict:
    """발화 → 접수카드. 직원용·보호자용 엔드포인트가 공유하는 실제 로직.

    긴급이면 카드를 만들지 않고 사람 연결로 전환한다.
    """
    if body.channel not in pipeline.CHANNELS:
        raise HTTPException(400, f"channel은 {pipeline.CHANNELS} 중 하나여야 합니다")

    res = pipeline.run(body.phone, body.utterance, channel=body.channel)
    out = res.to_dict()
    out["intake_id"] = None

    if body.save:
        if res.urgent:
            # 긴급도 기록은 남긴다 — 홈 대시보드의 '긴급' 카운트 근거
            class _Stub:
                target = res.profile["name"] if res.profile else "미확인"
                raw_utterance = body.utterance
                intent = "긴급"
                hospital = hospital_status = dept = None
                date_value = date_label = need_level = None
            out["intake_id"] = db.save_intake(_Stub(), body.phone, body.channel, status="긴급")
        elif res.card:
            status = "임시 접수" if res.profile is None else "접수 대기"
            out["intake_id"] = db.save_intake(res.card, body.phone, body.channel, status=status)
    # 카드를 만든 그 자리에서 확정 가능 여부까지 알려준다. 화면이 카드를 그리고
    # 나서 상세를 다시 부르지 않아도 되게.
    out["gate"] = gate.check(out.get("card"))
    return out


@app.post("/api/intakes", tags=["화면02 접수"], response_model=IntakeOut)
def create_intake(body: IntakeIn, user: dict = Depends(current_user)) -> dict:
    """직원(전화 상담·직접 접수)용 — 채널을 자유롭게 고를 수 있다. 로그인만 요구한다."""
    return _run_intake(body)


@app.post("/api/guardian/intakes", tags=["보호자 웹"], response_model=GuardianIntakeOut)
def guardian_create_intake(body: GuardianIntakeIn) -> dict:
    """보호자 웹 전용 — 무인증. channel은 항상 '앱·웹(보호자)'로 고정된다.

    로그인 없이 열려 있는 유일한 쓰기 API다. **응답은 GuardianIntakeOut 으로
    좁힌다** — 예전에는 직원용 응답을 그대로 돌려줘서, 토큰 없이 남의 번호만
    넣으면 그 어르신의 프로필과 진료 이력이 전부 나왔다.
    """
    res = _run_intake(IntakeIn(phone=body.phone, utterance=body.utterance,
                               channel="앱·웹(보호자)", save=True))
    # 화이트리스트로 옮겨 담는다. res 를 그대로 넘기고 response_model 로 거르는
    # 방식은 쓰지 않는다 — 모델에 필드가 하나 늘거나 extra 설정이 바뀌면 조용히
    # 다시 새기 때문이다. 여기서 명시적으로 고른 것만 나간다.
    return {
        "ok": True,
        "intake_id": res.get("intake_id"),
        "urgent": res.get("urgent", False),
        "urgent_confident": res.get("urgent_confident", True),
        "urgent_message": res.get("urgent_message"),
        "raw_utterance": body.utterance,
        "dept": res.get("dept"),        # analysis 값 — 발신자가 적은 문장에서 나온다
        "date": res.get("date"),        # 〃
    }


@app.post("/api/stt", tags=["화면02 접수"])
def transcribe(file: UploadFile = File(..., description="음성 파일 (wav/mp3/m4a/aiff)"),
              user: dict = Depends(current_user)) -> dict:
    """음성 → 텍스트. 확신도가 낮으면 needs_review=true로 사람 확인을 요구한다.

    async def가 아니라 def다. 전사는 CPU를 붙잡는 블로킹 작업이라
    async def로 두면 이벤트 루프에서 실행돼 전사 시간 내내 서버 전체가 멈춘다.
    (실측: 전사 중 /api/health 0.0006s → 0.96s) def면 스레드풀로 빠진다.
    """
    if not stt.available():
        raise HTTPException(503, "faster-whisper 미설치 — 텍스트 입력을 사용하세요")
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()
        return stt.transcribe(tmp.name).to_dict()
    except stt.AudioTooLong as e:
        # 413 — 받아서 죽느니 거절한다 (메모리가 음성 길이에 비례해 늘어난다)
        raise HTTPException(413, str(e)) from e
    finally:
        os.unlink(tmp.name)


@app.post("/api/intakes/from-audio", tags=["화면02 접수"])
def intake_from_audio(file: UploadFile = File(...), phone: str = Query(...),
                      channel: str = Query("전화"),
                      user: dict = Depends(current_user)) -> dict:
    """음성 파일 하나로 접수까지 — 시연 첫 장면(전화 시뮬레이션)용.

    transcribe/create_intake를 HTTP가 아니라 파이썬 함수로 직접 부른다 — 그래서
    이미 여기서 풀린 user를 그대로 넘긴다(각자 다시 로그인 헤더를 파싱하지 않는다).
    """
    tr = transcribe(file, user=user)
    res = create_intake(IntakeIn(phone=phone, utterance=tr["text"], channel=channel), user=user)
    res["stt"] = tr
    return res


@app.get("/api/intakes/{intake_id}", tags=["화면03 접수카드"])
def get_intake(intake_id: int, user: dict = Depends(current_user)) -> dict:
    if not db.can(user["role"], "intake.view"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 조회 권한이 없습니다")
    row = db.get_intake(intake_id)
    if not row:
        raise HTTPException(404, "접수를 찾을 수 없습니다")
    # 확정 버튼을 잠글지 말지는 서버가 정한다. 화면이 fields 를 훑어 스스로
    # 판단하면 화면마다 규칙이 갈라진다.
    row["gate"] = gate.check(row.get("card"))
    return row


@app.post("/api/intakes/{intake_id}/verify", tags=["화면03 접수카드"])
def verify_field(intake_id: int, body: VerifyIn, user: dict = Depends(current_user)) -> dict:
    """통화로 확인한 결과를 항목에 반영한다 — 게이트를 푸는 유일한 경로.

    AI 가 낸 값을 사람이 덮어쓰는 자리다. 무엇을 무엇으로 바꿨는지 감사 로그에
    남고, 카드 근거에도 '통화로 확인함'이 붙는다.
    """
    if not db.can(user["role"], "intake.confirm"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 확인 권한이 없습니다")
    if not db.get_intake(intake_id):
        raise HTTPException(404, "접수를 찾을 수 없습니다")
    row = db.verify_card_field(intake_id, body.field, body.value, user["name"], user["role"])
    if not row:
        # 카드 없는 접수(긴급)거나 확인 대상이 아닌 항목
        raise HTTPException(400, f"'{body.field}' 항목은 확인 입력을 받을 수 없습니다")
    row["gate"] = gate.check(row.get("card"))
    return {"ok": True, "intake": row}


@app.post("/api/intakes/{intake_id}/confirm", tags=["화면03 접수카드"])
def confirm(intake_id: int, body: ConfirmIn, user: dict = Depends(current_user)) -> dict:
    """사회복지사 확정 — 사람의 영역. 확정 이력은 감사 로그에 남는다.

    확인 필요가 남아 있으면 409 로 막고 무엇이 막는지를 돌려준다. 사회복지사가
    그래도 넘어가려면 acknowledge=true 를 보내야 하고, 그 사실이 감사 로그에
    남는다. 기관이 INTAKE_BLOCK_ALL_UNCONFIRMED 를 켜 두면 acknowledge 도
    통하지 않는다.
    """
    if not db.can(user["role"], "intake.confirm"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 확정 권한이 없습니다")
    row = db.get_intake(intake_id)
    if not row:
        raise HTTPException(404, "접수를 찾을 수 없습니다")

    g = gate.check(row.get("card"), body.acknowledge)
    if not g["allowed"]:
        # 422 가 아니라 409 다 — 요청이 잘못된 게 아니라 지금 상태에서 못 하는 것이다.
        raise HTTPException(409, detail={
            "message": "확인이 필요한 항목이 남아 있습니다",
            "gate": g,
        })

    db.confirm_intake(intake_id, body.hospital, body.date, body.level, user["name"], user["role"])
    if g["acknowledged"]:
        # 확인 없이 넘어간 것은 반드시 흔적이 남아야 한다. 나중에 문제가 생겼을 때
        # "누가 무엇을 확인하지 않고 확정했는가"에 답할 수 있어야 한다.
        db.log_audit(user["name"], user["role"], "미확인 확정", "intake", str(intake_id),
                     "확인 없이 확정: " + ", ".join(b["label"] for b in g["blockers"]))
    out = db.get_intake(intake_id)
    out["gate"] = gate.check(out.get("card"))
    return {"ok": True, "intake": out, "acknowledged": g["acknowledged"]}


@app.post("/api/intakes/{intake_id}/resolve", tags=["화면03 접수카드"])
def resolve_urgent(intake_id: int, body: ResolveIn, user: dict = Depends(current_user)) -> dict:
    """긴급 건 처리 완료 표시 — 사람이 연락을 끝냈다는 뜻이다.

    확정과 다르다. 긴급은 접수카드를 만들지 않으므로 확정할 대상이 없다.
    """
    if not db.can(user["role"], "intake.confirm"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 처리 권한이 없습니다")
    if not db.get_intake(intake_id):
        raise HTTPException(404, "접수를 찾을 수 없습니다")
    changed = db.resolve_urgent(intake_id, user["name"], user["role"], body.note)
    # changed=False 는 이미 처리됐거나 긴급이 아니라는 뜻 — 오류가 아니다
    return {"ok": True, "changed": changed, "intake": db.get_intake(intake_id)}


# ------------------------------------------- 화면 05 사후기록 --

@app.post("/api/post-records", tags=["화면05 사후기록"])
def create_post_record(body: PostRecordIn, user: dict = Depends(current_user)) -> dict:
    """음성 메모 → 기록 초안. 항상 '검토 필요' 상태로 사람에게 넘긴다."""
    draft = summarize.summarize(body.memo, target=body.target, dept=body.dept)
    rid = db.save_post_record(body.intake_id, body.phone, body.memo, draft.as_dict())
    return {
        "record_id": rid,
        "draft": draft.as_dict(),
        "needs_schedule_check": draft.needs_schedule_check,
        "source": draft.source,
        "notes": draft.notes,
    }


@app.post("/api/post-records/{record_id}/approve", tags=["화면05 사후기록"])
def approve_post_record(record_id: int, body: ApproveIn,
                        user: dict = Depends(current_user)) -> dict:
    """AI는 프로필을 자동 변경하지 않는다 — 승인한 항목만 반영된다."""
    if not db.can(user["role"], "post.approve"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 승인 권한이 없습니다")
    res = db.approve_post_record(record_id, body.approved, user["name"], user["role"])
    # changed=False 는 이미 같은 상태였다는 뜻이다(재요청·더블클릭). 오류가 아니므로
    # 200 으로 돌려주되, 프로필에 반영됐는지를 화면이 구분할 수 있게 함께 내린다.
    return {"ok": True, "approved": body.approved, **res}


@app.get("/api/post-records", tags=["화면05 사후기록"])
def list_post_records(limit: int = Query(50, le=200),
                      user: dict = Depends(current_user)) -> list[dict]:
    if not db.can(user["role"], "intake.view"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 조회 권한이 없습니다")
    return db.list_post_records(limit=limit)


# ------------------------------------------------ 감사 로그 / 자원 --

@app.get("/api/audit", tags=["감사 로그"])
def audit(limit: int = Query(100, le=500), user: dict = Depends(current_user)) -> list[dict]:
    if not db.can(user["role"], "audit.view"):
        raise HTTPException(403, f"'{user['role']}' 역할에는 감사 로그 조회 권한이 없습니다")
    return db.list_audit(limit=limit)


@app.get("/api/facilities", tags=["지역 자원"])
def facilities(region: str | None = None, query: str | None = None,
               limit: int = Query(10, le=50),
               user: dict = Depends(current_user)) -> list[dict]:
    """공공데이터 기반 복지자원 검색 — 결과에 출처(C-DS**)를 함께 반환한다."""
    return rag.search(region=region, query=query, limit=limit)


@app.post("/api/flywheel", tags=["지역 자원"])
def flywheel(phone: str = Body(...), date: str = Body(...), hospital: str = Body(...),
             dept: str = Body(...), user: dict = Depends(current_user)) -> dict:
    """동행 완료 → 이력 누적. 다음 접수의 병원 후보가 더 정확해진다.

    actor는 예전엔 본문에서 그대로 받았다(누구든 이름을 지어낼 수 있었다) —
    로그인 이름으로 바꿨다.
    """
    db.add_history(phone, date, hospital, dept)
    db.log_audit(user["name"], user["role"], "이력추가", "history", phone,
                f"{date} {hospital}({dept})")
    return {"ok": True}


def _warmup() -> dict:
    """예열 본체 — 의존성 없이 부를 수 있게 엔드포인트와 분리해 둔다.

    기동 예열(_schedule_warmup)이 이 함수를 파이썬 호출로 부른다. 엔드포인트를
    직접 부르면 FastAPI 의 의존성 주입이 돌지 않아서 user 에 Depends 객체가
    그대로 들어온다 — 지금은 본문이 user 를 안 읽어 무해하지만, 나중에 누가
    감사 로그 한 줄(`user["name"]`)만 넣어도 예열이 TypeError 로 죽고
    _schedule_warmup 의 except 가 그걸 삼켜서 조용히 안 데워진 채로 돈다.
    """
    import time

    from ..core import geo
    from ..services import airquality, intent_model, weather

    t0 = time.time()
    done: dict[str, str] = {}

    # 학습 모델 로드
    done["intent_model"] = ("BERT" if intent_model.bert_available()
                            else "TF-IDF" if intent_model.available() else "미학습")

    # 음성 인식 모델 로드.
    #
    # 예열 목록에서 빠져 있었다. 전화가 오면 /recording 안에서 처음 로드되는데,
    # 그동안 어르신은 통화 중에 그냥 기다린다(medium 기준 수십 초). 예열이
    # '시연 중 멈춤을 없애는 것' 이라면 여기가 가장 큰 구간이다.
    try:
        from ..services import stt
        if stt.available():
            stt._get_model()
            done["stt_model"] = "loaded"
        else:
            done["stt_model"] = "faster-whisper 미설치"
    except Exception as e:
        done["stt_model"] = f"{type(e).__name__}: {e}"[:120]

    # RAG 임베딩 모델 로드 + 인덱싱 (최초 20초가량 걸린다)
    try:
        from ..services import rag
        rag.search(region="광주광역시 서구", query="예열", limit=1)
        # 폴백으로 내려앉았으면 **이유까지** 싣는다. 예전에는 "폴백(토큰겹침)"
        # 한마디뿐이라, 패키지가 없는 건지 모델을 못 받은 건지 알 수 없었다.
        done["rag_embedding"] = ("loaded" if rag.available()
                                 else f"폴백(토큰겹침) — {rag.load_reason() or '원인 불명'}")
    except Exception as e:
        done["rag_embedding"] = f"{type(e).__name__}: {e}"[:120]

    # 시연에 쓰이는 지역만 예열
    for region in ("전남 고흥군", "전남 보성군", "광주광역시 서구"):
        latlon = geo.coords_of(region)
        if not latlon:
            continue
        try:
            w = weather.forecast(latlon[0], latlon[1])
            done[f"weather:{region}"] = "ok" if w.ok else (w.reason or "실패")
        except Exception as e:
            done[f"weather:{region}"] = type(e).__name__
        try:
            a = airquality.realtime(region)
            done[f"air:{region}"] = "ok" if a.ok else (a.reason or "실패")
        except Exception as e:
            done[f"air:{region}"] = type(e).__name__
        # 심평원도 예열한다. 이력 없는 대상자의 '거리 기준 참고 후보'(화면 04 4-A)가
        # 이 API를 타는데, 예열 목록에서 빠져 있어 시연 도중 첫 호출이 타임아웃 났다.
        # 캐시에 올려두면 발표 중에는 즉시 응답한다.
        try:
            from ..services import hira
            h = hira.nearby(latlon[0], latlon[1], dept=None,
                            radius_m=pipeline.REFERENCE_RADIUS_M,
                            rows=pipeline.REFERENCE_ROWS)
            done[f"hira:{region}"] = "ok" if h.ok else (h.reason or "실패")
        except Exception as e:
            done[f"hira:{region}"] = type(e).__name__

    return {"elapsed": round(time.time() - t0, 1), "warmed": done}


@app.post("/api/warmup", tags=["시스템"])
def warmup(user: dict = Depends(current_user)) -> dict:
    """시연 직전 예열 — 외부 API 응답을 캐시에 채우고 모델을 로드한다.

    기상·대기 API는 첫 호출이 수 초 걸린다. 발표 중 접수카드가 멈춰 보이지 않도록
    시작 전에 미리 불러 캐시에 올려둔다(실측: 예열 전 13.3s → 예열 후 즉시).
    """
    return _warmup()


def _via_internet(request: Request) -> bool:
    """이 요청이 Cloudflare 터널을 거쳐 들어왔는가.

    Cloudflare는 프록시한 요청에 CF-Connecting-IP / CF-Ray 를 붙인다. 클라이언트가
    이 헤더를 지울 수는 없고, 터널을 우회해 컨테이너에 직접 닿으려면 같은 로컬
    네트워크에 있어야 한다. 그래서 '외부에서 온 요청'의 판별 근거로 쓸 수 있다.
    """
    return bool(request.headers.get("cf-connecting-ip") or request.headers.get("cf-ray"))


@app.post("/api/reset", tags=["시스템"])
def reset(request: Request) -> dict:
    """데모 초기화. **외부(터널) 요청은 거부한다.**

    이 API에는 인증이 없어서, 막지 않으면 주소를 아는 사람이 시연 데이터를
    통째로 날릴 수 있다. 로컬(리허설 스크립트·본인 브라우저)에서는 그대로 된다.
    """
    if _via_internet(request):
        raise HTTPException(403, "외부에서는 초기화할 수 없습니다 (서버 로컬에서만 가능)")
    db.reset_db()
    return {"ok": True}


# --------------------------------------------- 개발 확인용 최소 UI --

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dev_ui(request: Request) -> str:
    """개발 확인용 단일 페이지. 실제 UI는 프론트 담당자가 별도 구현.

    외부(터널)에서 열면 '초기화' 버튼을 감춘다 — 눌러도 서버가 403으로 막지만,
    시연 중 심사위원이 누를 수 있는 버튼을 보여줄 이유가 없다.
    """
    reset_btn = ("" if _via_internet(request)
                 else '<button onclick="post(\'/api/reset\',{})">초기화</button>')
    return """<!doctype html><html lang=ko><meta charset=utf-8>
<title>동행고리 AI — dev</title>
<style>
body{font:14px/1.6 -apple-system,'Apple SD Gothic Neo',sans-serif;max-width:900px;margin:24px auto;padding:0 16px}
h1{font-size:18px} input,select,button,textarea{font:inherit;padding:6px 8px}
input[type=text]{width:100%} pre{background:#f6f6f6;padding:12px;border-radius:6px;overflow:auto;max-height:60vh}
.row{display:flex;gap:8px;margin:6px 0} .row>*{flex:1} button{cursor:pointer}
small{color:#666}
</style>
<h1>동행고리 AI — 개발 확인용</h1>
<small>실제 UI는 프론트 담당. 여기선 API 동작만 확인합니다. ·
<a href="/docs">Swagger 문서</a></small>

<h3>로그인</h3>
<small>대부분의 버튼은 이제 로그인이 필요합니다. 계정은
<code>python -m donghaenggori.services.create_user</code> 로 미리 만들어 둘 것.</small>
<div class=row>
  <input type=text id=email placeholder="이메일">
  <input type=password id=password placeholder="비밀번호">
  <button onclick="login()">로그인</button>
</div>
<div class=row><small id=who>로그인 안 됨</small></div>

<h3>접수</h3>
<div class=row>
  <input type=text id=phone value="010-1234-5678">
  <select id=channel><option>전화</option><option>앱·웹(보호자)</option><option>직접(기관)</option></select>
</div>
<div class=row><input type=text id=utt value="모레 정형외과 가야겄어"></div>
<div class=row>
  <button onclick="go()">접수카드 생성</button>
  <button onclick="load('/api/dashboard')">대시보드</button>
  <button onclick="load('/api/audit')">감사 로그</button>
  <button onclick="load('/api/status')">상태</button>
  __RESET_BTN__
</div>

<h3>음성 입력 (STT)</h3>
<small>파일 업로드 또는 마이크 녹음. 인식된 문장은 위 접수 칸에 자동으로 채워집니다.<br>
마이크는 localhost에서만 열립니다 — LAN IP(http)로 접속하면 브라우저가 막습니다.</small>
<div class=row>
  <input type=file id=audio accept="audio/*">
  <button onclick="sttFile()">파일 인식</button>
  <button onclick="fromAudio()">음성으로 바로 접수</button>
</div>
<div class=row>
  <button id=recbtn onclick="toggleRec()">● 녹음 시작</button>
  <span id=recstat><small>대기 중</small></span>
</div>

<h3>사후기록 요약</h3>
<div class=row><textarea id=memo rows=2>오늘 무릎 주사 맞았고, 다음 진료는 2주 뒤. 약국 들러서 약 받았어요. 계단 힘들어하셨습니다.</textarea></div>
<div class=row><button onclick="postRecord()">요약 생성</button></div>

<pre id=out>결과가 여기 표시됩니다.</pre>
<script>
const out = document.getElementById('out');
const who = document.getElementById('who');
let TOKEN = localStorage.getItem('token') || '';
const authHeaders = () => TOKEN ? {'Authorization': 'Bearer ' + TOKEN} : {};
const show = d => out.textContent = JSON.stringify(d, null, 2);
async function login(){
  const r = await fetch('/api/auth/login', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email: email.value, password: password.value})});
  const d = await r.json();
  show(d);
  if (r.ok) {
    TOKEN = d.token;
    localStorage.setItem('token', TOKEN);
    who.textContent = `${d.user.name} (${d.user.role}) 로그인됨`;
  } else {
    who.textContent = '로그인 실패';
  }
}
async function load(u){ show(await (await fetch(u, {headers: authHeaders()})).json()); }
async function post(u,b){ show(await (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(b)})).json()); }
function go(){ post('/api/intakes',{phone:phone.value,utterance:utt.value,channel:channel.value}); }
function postRecord(){ post('/api/post-records',{intake_id:0,phone:phone.value,memo:memo.value,dept:'정형외과',target:'박순자 어르신'}); }

// ── STT ────────────────────────────────────────────────────────
// 인식 결과는 접수 칸에 채워 넣는다 — 바로 '접수카드 생성'으로 이어서 확인하려고.
async function send(url, blob, name){
  const fd = new FormData(); fd.append('file', blob, name);
  out.textContent = '인식 중…';
  const r = await fetch(url, {method:'POST', headers: authHeaders(), body:fd});
  const d = await r.json();
  show(d);
  const t = d.text || (d.stt && d.stt.text);
  if (t) utt.value = t;
  return d;
}
function pick(){
  const f = document.getElementById('audio').files[0];
  if (!f) { out.textContent = '음성 파일을 먼저 고르세요.'; return null; }
  return f;
}
function sttFile(){ const f = pick(); if (f) send('/api/stt', f, f.name); }
function fromAudio(){
  const f = pick(); if (!f) return;
  send('/api/intakes/from-audio?phone=' + encodeURIComponent(phone.value)
       + '&channel=' + encodeURIComponent(channel.value), f, f.name);
}

let rec = null, chunks = [];
async function toggleRec(){
  const btn = document.getElementById('recbtn'), stat = document.getElementById('recstat');
  if (rec && rec.state === 'recording') { rec.stop(); return; }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    stat.innerHTML = '<small>이 브라우저·주소에서는 마이크를 쓸 수 없습니다. 파일 업로드를 쓰세요.</small>';
    return;
  }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({audio:true}); }
  catch (e) { stat.innerHTML = '<small>마이크 권한 거부됨: ' + e.name + '</small>'; return; }
  chunks = [];
  rec = new MediaRecorder(stream);
  rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    btn.textContent = '● 녹음 시작';
    stat.innerHTML = '<small>인식 중…</small>';
    const blob = new Blob(chunks, {type: rec.mimeType || 'audio/webm'});
    const d = await send('/api/stt', blob, 'rec.webm');
    stat.innerHTML = '<small>' + (d.needs_review ? '확신도 낮음 — 사람 확인 필요' : '완료') + '</small>';
  };
  rec.start();
  btn.textContent = '■ 녹음 정지';
  stat.innerHTML = '<small>녹음 중… 말한 뒤 정지를 누르세요</small>';
}
</script>
</html>""".replace("__RESET_BTN__", reset_btn)
