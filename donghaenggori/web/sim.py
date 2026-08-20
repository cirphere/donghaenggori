"""전화 시뮬레이터 — STT 를 건너뛰고 글로 통화를 재현한다.

    GET  /sim              화면 (인증 없음 — 안에서 로그인한다)
    POST /api/sim/start    전화가 걸려왔다 (발신번호만)
    POST /api/sim/say      어르신이 말했다 (성함·문의·후속답변 전부 이 하나로)

왜 필요한가
    실통화는 회선·녹음·전사를 거쳐야 한 턴이 끝난다. 되묻기 하나를 확인하려고
    매번 전화를 걸었고, 그때마다 **STT 가 잘못 들은 것**과 **흐름이 틀린 것**이
    섞여서 원인을 가르기 어려웠다. 여기서는 전사 결과를 직접 적으므로 흐름만
    따로 본다.

무엇을 재현하고 무엇을 재현하지 않는가
    재현한다   : 등록/미등록 분기 · 긴급 전환 · 되묻기(상한·사람연결 신호까지)
    재현 안 한다: STT · VAD · 음성 안내 · 담당자 실제 연결

    **voice.py 를 고치지 않는다.** 안내 문구와 되묻기 상한은 voice 에서 그대로
    읽어 오므로 저쪽을 바꾸면 여기도 따라간다. 판단은 voice 와 마찬가지로
    core(pipeline·followup) 가 한다 — 두 경로가 같은 함수를 본다.

접수는 **실제로 저장된다.** 화면·감사 로그에 그대로 남는다.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..core import db, gate, pipeline
from ..core import followup as fu_mod
from . import voice

_log = logging.getLogger("donghaenggori.sim")

# 인증이 붙는 쪽과 안 붙는 쪽을 나눈다. 화면은 로그인 상자를 그려야 하므로
# 인증 없이 열려야 하고, 접수를 만드는 API 는 /api/intakes 와 같은 조건이다.
# 의존성은 api.py 가 include 할 때 주입한다(여기서 api 를 import 하면 순환).
router = APIRouter(tags=["전화 시뮬레이터"])
page_router = APIRouter(include_in_schema=False)

# 진행 중인 모의 통화. voice._FOLLOWUP 과 같은 이유로 **DB 에 넣지 않는다** —
# 접수로 이어지지 못한 통화의 발화를 남기지 않는다.
_CALLS: dict[str, dict] = {}
_MAX_CALLS = 50


def _put(call: dict) -> None:
    if len(_CALLS) >= _MAX_CALLS:                     # 오래된 것부터 버린다
        _CALLS.pop(next(iter(_CALLS)), None)
    _CALLS[call["key"]] = call


def _get(key: str) -> dict:
    call = _CALLS.get(key or "")
    if call is None:
        raise HTTPException(404, "통화를 찾을 수 없습니다 — 다시 걸어 주세요")
    if call["phase"] == "end":
        raise HTTPException(409, "이미 끝난 통화입니다")
    return call


class StartIn(BaseModel):
    phone: str = Field(..., description="발신번호")


class SayIn(BaseModel):
    key: str = Field(..., description="통화 키")
    text: str = Field(..., description="어르신이 말한 것 — 전사문을 직접 적는다")


def _turn(call: dict, say: list[str], *, end: bool = False) -> dict:
    """한 턴의 응답. 화면이 그릴 것을 전부 담는다."""
    if end:
        call["phase"] = "end"
    card = g = None
    if call.get("intake_id"):
        card = (db.get_intake(call["intake_id"]) or {}).get("card")
        g = gate.check(card)
    return {
        "key": call["key"], "phase": call["phase"], "say": say, "end": end,
        "intake_id": call.get("intake_id"), "card": card, "gate": g,
        "asked": call.get("asked", 0), "followup_max": voice.FOLLOWUP_MAX,
        "question": call["pending"].question if call.get("pending") else None,
        "question_field": call["pending"].field if call.get("pending") else None,
    }


@router.post("/api/sim/start")
def start(body: StartIn) -> dict:
    """전화가 걸려왔다. voice.incoming 과 같은 분기다."""
    phone = voice._lookup_phone(body.phone)
    prof = db.get_profile(phone)
    call = {"key": secrets.token_urlsafe(8), "phone": phone, "raw_phone": body.phone,
            "identity": None, "intake_id": None, "asked": 0,
            "state": fu_mod.CallState(), "pending": None, "original": ""}
    _put(call)
    _log.info("[sim] 발신 %s → %s", body.phone,
              prof["name"] if prof else "등록 없음")

    # 미등록이거나 **이름을 모르는 프로필** — 성함·읍면동부터 받는다.
    if not prof or not voice._has_real_name(prof):
        call["phase"] = "identity"
        return _turn(call, [voice.GREETING, voice.WHO_PROMPT])

    call["phase"] = "symptom"
    return _turn(call, [voice.SYMPTOM_PROMPT])


@router.post("/api/sim/say")
def say(body: SayIn) -> dict:
    """어르신이 말했다. 지금 단계에 따라 성함·문의·후속답변으로 갈린다."""
    call = _get(body.key)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "말한 내용이 비어 있습니다")

    if call["phase"] == "identity":
        # 성함·읍면동. **문의 원문과 섞지 않는다** — voice 와 같은 이유다.
        call["identity"] = text
        call["phase"] = "symptom"
        return _turn(call, [voice.SYMPTOM_PROMPT])

    if call["phase"] == "symptom":
        return _symptom(call, text)

    return _followup(call, text)


def _symptom(call: dict, text: str) -> dict:
    """문의 내용. voice.recording 의 뒷부분과 같다."""
    res = pipeline.run(call["phone"], text, channel="전화",
                       identity_utterance=call.get("identity"))
    if res.urgent:
        call["intake_id"] = voice._save(res, call["phone"], text)
        _log.info("[sim] 긴급 — 카드 없이 담당자 연결")
        return _turn(call, ["긴급한 상황으로 보입니다.",
                            "담당자에게 바로 연결해 드리겠습니다. 잠시만 기다려 주세요.",
                            "〔담당자 연결 — 시뮬레이터에서는 여기까지〕"], end=True)

    call["intake_id"] = voice._save(res, call["phone"], text)
    call["original"] = text
    card = (db.get_intake(call["intake_id"]) or {}).get("card") if call["intake_id"] else None

    q = _next(call, card)
    if q is None:
        return _turn(call, [voice._receipt_card(card), voice.BYE], end=True)
    call["phase"] = "followup"
    return _turn(call, [q.question])


def _followup(call: dict, answer: str) -> dict:
    """후속질문의 답. voice.followup 과 순서가 같다 — **사람 연결 신호가 먼저다.**"""
    q = call["pending"]
    state: fu_mod.CallState = call["state"]
    intake_id = call["intake_id"]

    # ① 사람 연결 신호가 먼저다. 여기서 한 번 더 캐물으면 안 된다.
    h = fu_mod.detect_handoff_signal(answer, state)
    if h.needed:
        db.apply_followup(intake_id, q.field, q.question, answer,
                          evidence=[f"후속질문에 '{answer}'라고 답함 — {h.reason}"])
        db.stop_followup(intake_id, f"통화 중 사람 연결 신호 — {h.reason} "
                         "· 남은 항목은 사회복지사 확인 필요")
        call["pending"] = None
        if h.explicit:
            return _turn(call, ["네, 알겠습니다.",
                                "〔담당자 연결 — 시뮬레이터에서는 여기까지〕"], end=True)
        return _turn(call, ["네, 알겠습니다. 담당자가 확인 후 연락드리겠습니다.",
                            voice.BYE], end=True)

    # ② 그 칸 하나만 다시 뽑는다.
    card = (db.get_intake(intake_id) or {}).get("card") or {}
    r = fu_mod.reextract_field(q.field, call["original"], answer, card, q.question)
    state.record(q.field, answer, clear=r.resolved)
    db.apply_followup(intake_id, q.field, q.question, answer,
                      value=r.value, status=r.status, evidence=r.evidence,
                      downgrade=r.downgrade)
    call["asked"] += 1

    # ③ 상한까지 남았으면 다음 칸을 묻는다. 갱신된 카드로 다시 본다.
    card = (db.get_intake(intake_id) or {}).get("card") or {}
    if call["asked"] < voice.FOLLOWUP_MAX:
        nxt = _next(call, card)
        if nxt is not None:
            return _turn(call, [nxt.question])

    call["pending"] = None
    left = fu_mod.pending_fields(card, tuple(state.asked))
    if left:
        # 남은 것은 그대로 '확인 필요' 로 둔다. 우리가 채우지 않는다.
        db.stop_followup(intake_id, f"후속질문 {call['asked']}회로 마침 — "
                         f"남은 항목({', '.join(left)})은 사회복지사 콜백 필요")
    return _turn(call, [voice._receipt_card(card), voice.BYE], end=True)


def _next(call: dict, card: dict | None):
    """다음 되물을 것. 없으면 None — voice 와 같은 조건으로 건너뛴다."""
    if voice.FOLLOWUP_MAX <= 0 or not card or not call.get("intake_id"):
        call["pending"] = None
        return None
    q = fu_mod.next_question(card, tuple(call["state"].asked))
    call["pending"] = q
    if q is None:
        _log.info("[sim] 되물을 것 없음 — 남은 칸 %s",
                  fu_mod.pending_fields(card, tuple(call["state"].asked)) or "없음")
    return q


_PAGE = """<!doctype html><html lang=ko><meta charset=utf-8>
<title>전화 시뮬레이터 — 동행고리 AI</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#1a1a1a;--dim:#6b6b6b;--line:#e3e3e3;--ok:#0a7d3f;
      --warn:#8a6100;--need:#a11;--them:#f1f1f3;--us:#dbeafe}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8e8ea;--dim:#9a9aa2;
      --line:#2c2f36;--them:#24262c;--us:#1e3a5f;--ok:#5fd08a;--warn:#e0b45f;--need:#f08a8a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.65 -apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:19px;margin:0 0 2px}
.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.cols{display:grid;grid-template-columns:1fr 380px;gap:20px}
@media(max-width:860px){.cols{grid-template-columns:1fr}}
.panel{border:1px solid var(--line);border-radius:10px;padding:14px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input,button,select{font:inherit;padding:7px 10px;border-radius:7px;
     border:1px solid var(--line);background:var(--bg);color:var(--fg)}
input{flex:1;min-width:120px} button{cursor:pointer;background:var(--fg);color:var(--bg);border:0}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.4;cursor:not-allowed}
#log{min-height:300px;max-height:52vh;overflow-y:auto;display:flex;
     flex-direction:column;gap:9px;padding:4px 2px}
.msg{max-width:82%;padding:9px 12px;border-radius:13px;white-space:pre-wrap;word-break:break-word}
.them{background:var(--them);align-self:flex-start;border-bottom-left-radius:4px}
.us{background:var(--us);align-self:flex-end;border-bottom-right-radius:4px}
.sys{align-self:center;color:var(--dim);font-size:12.5px;text-align:center;max-width:95%}
.q{border-left:3px solid var(--warn);padding-left:9px}
h2{font-size:13px;color:var(--dim);margin:0 0 9px;font-weight:600;letter-spacing:.03em}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td{padding:5px 0;border-bottom:1px solid var(--line);vertical-align:top}
td.k{color:var(--dim);width:74px} td.v{font-weight:600}
.b{font-size:11.5px;padding:1px 6px;border-radius:20px;border:1px solid currentColor;
   white-space:nowrap;font-weight:500}
.s0{color:var(--ok)} .s1{color:var(--warn)} .s2{color:var(--need)}
.hint{color:var(--dim);font-size:12.5px;margin-top:10px;line-height:1.5}
code{background:var(--them);padding:1px 5px;border-radius:4px;font-size:12.5px}
</style>
<div class=wrap>
<h1>전화 시뮬레이터</h1>
<div class=sub>음성인식을 건너뛰고 <b>전사문을 직접 적어</b> 통화 흐름만 확인합니다.
접수는 실제로 저장됩니다.</div>

<div class=bar>
  <input id=uid placeholder="아이디" autocapitalize=off>
  <input id=pw type=password placeholder="비밀번호">
  <button onclick=login()>로그인</button>
  <span id=who class=hint style=align-self:center>로그인 안 됨</span>
</div>

<div class=cols>
<div class=panel>
  <div class=bar>
    <input id=phone placeholder="발신번호" value="010-1234-5678">
    <button onclick=start()>전화 걸기</button>
    <button class=ghost onclick=reset()>끊기</button>
  </div>
  <div id=log></div>
  <div class=bar style="margin:12px 0 0">
    <input id=say placeholder="전화 걸기부터 하세요" disabled
           onkeydown="if(event.key==='Enter')send()">
    <button id=sendbtn onclick=send() disabled>말하기</button>
  </div>
  <div class=hint>전사문을 그대로 적으세요 — STT 가 잘못 들은 것까지 그대로 적으면
    그 상태의 흐름을 볼 수 있습니다.</div>
</div>

<div class=panel>
  <h2>접수카드</h2>
  <div id=card class=hint>아직 없습니다</div>
</div>
</div>
</div>

<script>
let TOKEN='', KEY=null;
const $=id=>document.getElementById(id);

async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json',
    ...(TOKEN?{Authorization:'Bearer '+TOKEN}:{})},body:JSON.stringify(body)});
  const t=await r.text(); let j; try{j=JSON.parse(t)}catch{j={detail:t}}
  if(!r.ok) throw new Error(j.detail||r.status); return j;
}
async function login(){
  try{const j=await api('/api/auth/login',{user_id:$('uid').value,password:$('pw').value});
    TOKEN=j.token||j.access_token||''; $('who').textContent=(j.name||j.user_id||'로그인됨')+' 님';
    $('pw').value='';
  }catch(e){$('who').textContent='로그인 실패 — '+e.message}
}
function add(cls,text){const d=document.createElement('div');
  d.className='msg '+cls; d.textContent=text; $('log').appendChild(d);
  $('log').scrollTop=$('log').scrollHeight;}

function draw(j){
  (j.say||[]).forEach(s=>add(j.question&&s===j.question?'them q':'them',s));
  if(j.end){add('sys','— 통화 종료 —'); lock(true);}
  card(j);
}
function lock(off){$('say').disabled=off; $('sendbtn').disabled=off;
  $('say').placeholder=off?'전화 걸기부터 하세요':'어르신이 말한 것을 적으세요';
  if(!off)$('say').focus();}

const BADGE={'확인됨':'s0','추정':'s1','확인 필요':'s2'};
function card(j){
  const c=j.card; if(!c){$('card').className='hint';
    $('card').textContent=j.end?'카드 없음 (긴급 접수)':'아직 없습니다'; return;}
  const f=c.fields||{}; let h='<table>';
  for(const k in f){const v=f[k];
    h+=`<tr><td class=k>${v.label||k}</td><td class=v>${v.value??'—'}`
     + ` <span class="b ${BADGE[v.status]||''}">${v.status}</span>`
     + (v.spoken?`<div class=hint style=margin:2px_0_0>어르신 표현: ‘${v.spoken}’</div>`:'')
     + '</td></tr>';}
  h+='</table>';
  if(j.gate) h+=`<div class=hint>확정 ${j.gate.allowed?'가능':'막힘 — '
     +(j.gate.blockers||[]).map(b=>b.label||b.field||b).join(', ')}</div>`;
  if(j.asked) h+=`<div class=hint>되묻기 ${j.asked}/${j.followup_max}회</div>`;
  if(j.intake_id) h+=`<div class=hint>접수 #${j.intake_id}</div>`;
  $('card').className=''; $('card').innerHTML=h;
}

async function start(){
  if(!TOKEN){add('sys','먼저 로그인하세요');return}
  $('log').innerHTML=''; $('card').className='hint'; $('card').textContent='아직 없습니다';
  try{const j=await api('/api/sim/start',{phone:$('phone').value});
    KEY=j.key; add('sys','📞 '+$('phone').value+' 에서 전화가 왔습니다');
    draw(j); if(!j.end) lock(false);
  }catch(e){add('sys','실패 — '+e.message)}
}
async function send(){
  const t=$('say').value.trim(); if(!t||!KEY)return;
  add('us',t); $('say').value=''; lock(true);
  try{const j=await api('/api/sim/say',{key:KEY,text:t});
    draw(j); if(!j.end) lock(false);
  }catch(e){add('sys','실패 — '+e.message); lock(false)}
}
function reset(){KEY=null; lock(true); add('sys','— 끊었습니다 —');}
</script>
"""


@page_router.get("/sim", response_class=HTMLResponse)
def page() -> str:
    """시뮬레이터 화면. 인증은 안에서 한다(로그인 상자를 그려야 하므로)."""
    return _PAGE
