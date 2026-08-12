// 임시 화면의 렌더링. 계약(docs/FRONTEND.md)의 규칙만 지키고 꾸미지는 않았다.
//
// 지켜야 하는 것 — 제대로 만들 때도 그대로다.
//   1. 상태는 확인됨/추정/확인 필요 3단계. 확률(%)로 바꾸지 않는다.
//   2. 근거(evidence)를 반드시 함께 보여준다.
//   3. 긴급이면 카드를 그리지 않는다. urgent_confident 로 문구 강도를 나눈다.
//   4. 오전·오후를 모르는 시각은 화면이 임의로 채우지 않는다.
//   5. profile_update 는 제안일 뿐 — 승인 전에는 반영하지 않는다.

import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

// 상태값 → 뱃지 색. 계약에 없는 값이 오면 회색으로 두고 그대로 보여준다.
const STATUS_CLASS = { "확인됨": "ok", "추정": "guess", "확인 필요": "need" };

// ── 시스템 상태 ────────────────────────────────────────────
async function loadStatus() {
  try {
    const s = await api.status();
    const fac = Object.entries(s.facilities || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
    $("sysStatus").textContent = `의도분류 ${s.intent_model} · STT ${s.stt.model}/${s.stt.device} · 복지시설 ${fac}`;
    if (s.intent_model_fallback_reason) {
      $("sysStatus").title = s.intent_model_fallback_reason;   // 조용한 폴백을 눈에 보이게
      $("sysStatus").classList.add("warn");
    }
  } catch (e) {
    $("sysStatus").textContent = "백엔드 연결 실패 — " + e.message;
    $("sysStatus").classList.add("bad");
  }
}

// ── 접수카드 렌더 ──────────────────────────────────────────
function renderField(key, f) {
  const box = el("div", "field");
  const head = el("div", "field-head");
  head.append(el("span", "field-label", f.label));
  head.append(el("span", "badge " + (STATUS_CLASS[f.status] || ""), f.status));
  box.append(head);

  // 값이 없으면 비워두고 '확인 필요'로 남긴다 — 화면이 채워 넣지 않는다
  let shown = f.value || "—";
  if (f.spoken && f.value) shown = `${f.value}  (“${f.spoken}”)`;
  else if (f.spoken && !f.value) shown = `“${f.spoken}” — 오전·오후 확인 필요`;
  box.append(el("div", "field-value" + (f.value ? "" : " missing"), shown));

  if (f.evidence?.length) {
    const ul = el("ul", "evidence");
    f.evidence.forEach((e) => ul.append(el("li", null, e)));
    box.append(ul);
  }
  return box;
}

function renderUrgent(d) {
  const wrap = el("div", "urgent " + (d.urgent_confident ? "hard" : "soft"));
  wrap.append(el("div", "urgent-title",
    d.urgent_confident ? "긴급 신호 감지 — 접수 중단" : "발화를 이해하지 못함 — 담당자 확인"));
  wrap.append(el("p", null, d.urgent_message || ""));
  wrap.append(el("p", "small",
    d.urgent_confident
      ? "동행고리 AI는 응급 여부를 판단하지 않습니다. 사람에게 연결하세요."
      : "긴급 가능성을 배제하지 않았습니다. 접수카드는 만들지 않았습니다."));
  return wrap;
}

function renderCard(d) {
  const c = d.card;
  const frag = document.createDocumentFragment();

  frag.append(el("div", "summary", c.summary));
  const meta = el("div", "meta");
  meta.append(el("span", null, `${c.target} (${c.phone_masked})`));
  meta.append(el("span", null, `${d.channel} · ${d.intent}`));
  meta.append(el("span", "src", `판정 근거: ${d.intent_source}` +
    (d.intent_confidence != null ? ` (${d.intent_confidence.toFixed(2)})` : "")));
  frag.append(meta);
  frag.append(el("blockquote", null, `“${c.raw_utterance}”`));

  if (c.flags?.length) {
    const f = el("div", "flags");
    c.flags.forEach((x) => f.append(el("span", "flag", x)));
    frag.append(f);
  }

  // 항목별 값·상태·근거 — 카드의 본문
  const grid = el("div", "fields");
  for (const [k, v] of Object.entries(c.fields || {})) grid.append(renderField(k, v));
  frag.append(grid);

  // 동행 지원 수준 — 공식 판정인지 우리 추정인지 밝힌다
  const need = el("div", "block");
  need.append(el("h3", null, "동행 지원 수준(후보)"));
  const nb = el("div", "need");
  nb.append(el("b", null, c.need_level));
  nb.append(el("span", "badge " + (c.need_official ? "ok" : "need"),
    `${c.need_basis}${c.need_official ? "" : " · 임시 추정"}`));
  need.append(nb);
  if (c.need_reasons?.length) {
    const ul = el("ul", "evidence");
    c.need_reasons.forEach((r) => ul.append(el("li", null, r)));
    need.append(ul);
  }
  frag.append(need);

  if (c.confirm_questions?.length) {
    const q = el("div", "block");
    q.append(el("h3", null, "확인 질문 (콜백용)"));
    const ul = el("ul", "questions");
    c.confirm_questions.forEach((x) => ul.append(el("li", null, x)));
    q.append(ul);
    frag.append(q);
  }

  if (c.target_candidates?.length) {
    const t = el("div", "block");
    t.append(el("h3", null, `대상자 후보 ${c.target_candidates.length}명 — 확정 아님`));
    const ul = el("ul", "questions");
    c.target_candidates.forEach((x) =>
      ul.append(el("li", null, `${x.name} (${x.region}) — 보호자 ${x.guardian_name || "?"} ${x.guardian_relation || ""}`)));
    t.append(ul);
    frag.append(t);
  }

  if (c.reference_candidates?.length) {
    const r = el("div", "block");
    r.append(el("h3", null, "참고 후보 — 거리 기준, 확정 후보 아님"));
    const ul = el("ul", "questions");
    c.reference_candidates.forEach((h) =>
      ul.append(el("li", null, `${h.name} (${Math.round(h.distance_m)}m) — ${h.basis}`)));
    r.append(ul);
    frag.append(r);
  }

  if (c.outing_checklist?.length) {
    const o = el("div", "block");
    o.append(el("h3", null, "외출 전 참고"));
    const ul = el("ul", "questions");
    c.outing_checklist.forEach((x) => ul.append(el("li", null, x)));
    o.append(ul);
    frag.append(o);
  }

  // 지역 복지자원 — 관내인지 아닌지를 반드시 표시한다
  if (d.facilities?.length) {
    const f = el("div", "block");
    f.append(el("h3", null, "지역 복지자원"));
    const ul = el("ul", "facilities");
    d.facilities.forEach((x) => {
      const li = el("li");
      li.append(el("span", "badge " + (x.region_match === "관내" ? "ok" : "guess"), x.region_match));
      li.append(el("span", null, ` ${x.name} — ${x.basis} [${x.source}]`));
      ul.append(li);
    });
    f.append(ul);
    frag.append(f);
  }

  if (d.policy) {
    frag.append(el("div", "policy",
      `AI 범위: ${d.policy.ai_scope} · 의료 판단 ${d.policy.medical_judgement ? "함" : "안 함"}` +
      ` · 사람 검토 ${d.policy.human_review_required ? "필수" : "불필요"}`));
  }
  return frag;
}

function render(d) {
  const out = $("result");
  out.className = "";
  out.replaceChildren(d.urgent ? renderUrgent(d) : renderCard(d));
  if (d.stt) {
    const s = el("div", "stt" + (d.stt.needs_review ? " warn" : ""),
      `STT: “${d.stt.text}”` + (d.stt.needs_review ? " — 확신도 낮음, 사람 확인 필요" : ""));
    out.prepend(s);
  }
}

async function submit(fn) {
  const out = $("result");
  out.className = "loading";
  out.textContent = "분석 중…";
  try {
    render(await fn());
  } catch (e) {
    out.className = "bad";
    out.textContent = "오류: " + e.message;
  }
}

// ── 입력 ───────────────────────────────────────────────────
$("btnIntake").onclick = () =>
  submit(() => api.createIntake($("phone").value, $("utterance").value, $("channel").value));

$("btnPick").onclick = () => $("audioFile").click();
$("audioFile").onchange = (e) => {
  const f = e.target.files[0];
  if (f) submit(() => api.intakeFromAudio(f, $("phone").value, $("channel").value));
};

document.querySelectorAll(".sample").forEach((b) => {
  b.onclick = () => {
    $("phone").value = b.dataset.phone;
    $("utterance").value = b.dataset.text;
    $("btnIntake").click();
  };
});

// 녹음 — https 또는 localhost 에서만 마이크를 쓸 수 있다
let rec = null, chunks = [];
$("btnRec").onclick = async () => {
  const btn = $("btnRec"), hint = $("recHint");
  if (rec && rec.state === "recording") { rec.stop(); return; }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    hint.textContent = "이 브라우저·주소에서는 마이크를 쓸 수 없습니다. 음성 파일을 쓰세요.";
    return;
  }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { hint.textContent = "마이크 권한 거부됨: " + e.name; return; }

  chunks = [];
  rec = new MediaRecorder(stream);
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    btn.textContent = "● 녹음";
    hint.textContent = "";
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    blob.name = "rec.webm";
    submit(() => api.intakeFromAudio(blob, $("phone").value, $("channel").value));
  };
  rec.start();
  btn.textContent = "■ 정지";
  hint.textContent = "녹음 중… 말한 뒤 정지를 누르세요";
};

loadStatus();
