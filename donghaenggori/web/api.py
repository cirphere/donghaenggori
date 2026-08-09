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

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..core import db, pipeline
from ..services import rag, stt, summarize

app = FastAPI(
    title="동행고리 AI",
    description="사회복지사를 위한 병원동행 접수·이력정리 Copilot — AI는 후보·근거만, 확정은 사람",
    version="0.1.0",
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ------------------------------------------------------------ 스키마 --

class IntakeIn(BaseModel):
    phone: str = Field(..., description="발신번호 — 보조 식별 단서일 뿐, 대상자 확정 아님")
    utterance: str = Field(..., description="발화 텍스트 (STT 결과 또는 직접 입력)")
    channel: str = Field("전화", description="전화 | 앱·웹(보호자) | 직접(기관)")
    save: bool = Field(True, description="접수 기록으로 저장할지")


class ConfirmIn(BaseModel):
    hospital: str
    date: str
    level: str
    actor: str = "김○○ 사회복지사"
    role: str = "사회복지사"


class PostRecordIn(BaseModel):
    intake_id: int
    phone: str
    memo: str = Field(..., description="동행 매니저 음성 메모(텍스트)")
    dept: str | None = None
    target: str | None = None


class ApproveIn(BaseModel):
    approved: bool = True
    actor: str = "김○○ 사회복지사"
    role: str = "사회복지사"


# ------------------------------------------------------------ 상태 --

@app.get("/api/health", tags=["시스템"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status", tags=["시스템"])
def status() -> dict:
    """외부 연동 상태 — 키 값은 노출하지 않고 존재 여부만."""
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
def dashboard() -> dict:
    return {"counts": db.intake_counts(), "intakes": db.list_intakes(limit=50)}


@app.get("/api/intakes", tags=["화면01 홈"])
def list_intakes(limit: int = Query(50, le=200)) -> list[dict]:
    return db.list_intakes(limit=limit)


# ------------------------------------ 화면 02 접수 → 화면 03 카드 --

@app.post("/api/intakes", tags=["화면02 접수"])
def create_intake(body: IntakeIn) -> dict:
    """발화 → 접수카드. 긴급이면 카드를 만들지 않고 사람 연결로 전환한다."""
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
    return out


@app.post("/api/stt", tags=["화면02 접수"])
def transcribe(file: UploadFile = File(..., description="음성 파일 (wav/mp3/m4a/aiff)")) -> dict:
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
        raise HTTPException(413, str(e))
    finally:
        os.unlink(tmp.name)


@app.post("/api/intakes/from-audio", tags=["화면02 접수"])
def intake_from_audio(file: UploadFile = File(...), phone: str = Query(...),
                      channel: str = Query("전화")) -> dict:
    """음성 파일 하나로 접수까지 — 시연 첫 장면(전화 시뮬레이션)용."""
    tr = transcribe(file)
    res = create_intake(IntakeIn(phone=phone, utterance=tr["text"], channel=channel))
    res["stt"] = tr
    return res


@app.get("/api/intakes/{intake_id}", tags=["화면03 접수카드"])
def get_intake(intake_id: int) -> dict:
    row = db.get_intake(intake_id)
    if not row:
        raise HTTPException(404, "접수를 찾을 수 없습니다")
    return row


@app.post("/api/intakes/{intake_id}/confirm", tags=["화면03 접수카드"])
def confirm(intake_id: int, body: ConfirmIn) -> dict:
    """사회복지사 확정 — 사람의 영역. 확정 이력은 감사 로그에 남는다."""
    if not db.can(body.role, "intake.confirm"):
        raise HTTPException(403, f"'{body.role}' 역할에는 확정 권한이 없습니다")
    if not db.get_intake(intake_id):
        raise HTTPException(404, "접수를 찾을 수 없습니다")
    db.confirm_intake(intake_id, body.hospital, body.date, body.level, body.actor, body.role)
    return {"ok": True, "intake": db.get_intake(intake_id)}


# ------------------------------------------- 화면 05 사후기록 --

@app.post("/api/post-records", tags=["화면05 사후기록"])
def create_post_record(body: PostRecordIn) -> dict:
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
def approve_post_record(record_id: int, body: ApproveIn) -> dict:
    """AI는 프로필을 자동 변경하지 않는다 — 승인한 항목만 반영된다."""
    if not db.can(body.role, "post.approve"):
        raise HTTPException(403, f"'{body.role}' 역할에는 승인 권한이 없습니다")
    db.approve_post_record(record_id, body.approved, body.actor, body.role)
    return {"ok": True, "approved": body.approved}


@app.get("/api/post-records", tags=["화면05 사후기록"])
def list_post_records(limit: int = Query(50, le=200)) -> list[dict]:
    return db.list_post_records(limit=limit)


# ------------------------------------------------ 감사 로그 / 자원 --

@app.get("/api/audit", tags=["감사 로그"])
def audit(limit: int = Query(100, le=500), role: str = "사회복지사") -> list[dict]:
    if not db.can(role, "audit.view"):
        raise HTTPException(403, f"'{role}' 역할에는 감사 로그 조회 권한이 없습니다")
    return db.list_audit(limit=limit)


@app.get("/api/facilities", tags=["지역 자원"])
def facilities(region: str | None = None, query: str | None = None,
               limit: int = Query(10, le=50)) -> list[dict]:
    """공공데이터 기반 복지자원 검색 — 결과에 출처(C-DS**)를 함께 반환한다."""
    return rag.search(region=region, query=query, limit=limit)


@app.post("/api/flywheel", tags=["지역 자원"])
def flywheel(phone: str = Body(...), date: str = Body(...), hospital: str = Body(...),
             dept: str = Body(...), actor: str = Body("최정미 동행매니저")) -> dict:
    """동행 완료 → 이력 누적. 다음 접수의 병원 후보가 더 정확해진다."""
    db.add_history(phone, date, hospital, dept)
    db.log_audit(actor, "동행매니저", "이력추가", "history", phone, f"{date} {hospital}({dept})")
    return {"ok": True}


@app.post("/api/warmup", tags=["시스템"])
def warmup() -> dict:
    """시연 직전 예열 — 외부 API 응답을 캐시에 채우고 모델을 로드한다.

    기상·대기 API는 첫 호출이 수 초 걸린다. 발표 중 접수카드가 멈춰 보이지 않도록
    시작 전에 미리 불러 캐시에 올려둔다(실측: 예열 전 13.3s → 예열 후 즉시).
    """
    import time
    from ..core import geo
    from ..services import airquality, intent_model, weather

    t0 = time.time()
    done: dict[str, str] = {}

    # 학습 모델 로드
    done["intent_model"] = ("BERT" if intent_model.bert_available()
                            else "TF-IDF" if intent_model.available() else "미학습")

    # RAG 임베딩 모델 로드 + 인덱싱 (최초 20초가량 걸린다)
    try:
        from ..services import rag
        rag.search(region="광주광역시 서구", query="예열", limit=1)
        done["rag_embedding"] = "loaded" if rag.available() else "폴백(토큰겹침)"
    except Exception as e:
        done["rag_embedding"] = type(e).__name__

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

    return {"elapsed": round(time.time() - t0, 1), "warmed": done}


@app.post("/api/reset", tags=["시스템"])
def reset() -> dict:
    db.reset_db()
    return {"ok": True}


# --------------------------------------------- 개발 확인용 최소 UI --

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dev_ui() -> str:
    """개발 확인용 단일 페이지. 실제 UI는 프론트 담당자가 별도 구현."""
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
  <button onclick="post('/api/reset',{})">초기화</button>
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
const show = d => out.textContent = JSON.stringify(d, null, 2);
async function load(u){ show(await (await fetch(u)).json()); }
async function post(u,b){ show(await (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json()); }
function go(){ post('/api/intakes',{phone:phone.value,utterance:utt.value,channel:channel.value}); }
function postRecord(){ post('/api/post-records',{intake_id:0,phone:phone.value,memo:memo.value,dept:'정형외과',target:'박순자 어르신'}); }

// ── STT ────────────────────────────────────────────────────────
// 인식 결과는 접수 칸에 채워 넣는다 — 바로 '접수카드 생성'으로 이어서 확인하려고.
async function send(url, blob, name){
  const fd = new FormData(); fd.append('file', blob, name);
  out.textContent = '인식 중…';
  const r = await fetch(url, {method:'POST', body:fd});
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
</html>"""
