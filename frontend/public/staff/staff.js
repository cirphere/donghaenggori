// 사회복지사 콘솔 — 접수 확인·확정, 사후기록 승인, 감사 로그.
//
// 보호자 웹과 나눈 이유는 화면 구성이 아니라 **권한**이다. 여기서만 확정·승인·
// 감사로그 조회를 한다. 다만 백엔드가 아직 요청 본문의 role 을 그대로 믿으므로,
// 이 파일이 권한을 지켜주지는 못한다 — 실제 경계는 Cloudflare Access 다.
// 인증이 붙으면 ROLE/ACTOR 를 서버가 준 신원으로 바꾸고 이 상수는 지운다.

import { api } from "../api.js";

const ROLE = "사회복지사";
const ACTOR = "김○○ 사회복지사";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const STATUS_CLASS = { "확인됨": "ok", "추정": "guess", "확인 필요": "need" };

// ── 탭 ─────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".view").forEach((x) => x.classList.add("hidden"));
    t.classList.add("active");
    $("view-" + t.dataset.view).classList.remove("hidden");
    if (t.dataset.view === "queue") loadQueue();
    if (t.dataset.view === "audit") loadAudit();
  };
});

// ── 상태 ───────────────────────────────────────────────────
async function loadStatus() {
  try {
    const s = await api.status();
    const fac = Object.entries(s.facilities || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
    $("sysStatus").textContent =
      `의도분류 ${s.intent_model} · STT ${s.stt.model}/${s.stt.device} · 복지시설 ${fac}`;
    if (s.intent_model_fallback_reason) {
      $("sysStatus").title = s.intent_model_fallback_reason;
      $("sysStatus").classList.add("warn");
    }
  } catch (e) {
    $("sysStatus").textContent = "백엔드 연결 실패 — " + e.message;
    $("sysStatus").classList.add("bad");
  }
}

// ── 접수 대기 ──────────────────────────────────────────────
async function loadQueue() {
  const box = $("counts"), tb = $("queue").querySelector("tbody");
  try {
    const d = await api.dashboard();
    box.replaceChildren();
    for (const [k, v] of Object.entries(d.counts || {})) {
      const c = el("div", "count");
      c.append(el("b", null, String(v)));
      c.append(el("span", null, k));
      if (k === "urgent" && v > 0) c.classList.add("alarm");
      box.append(c);
    }
    tb.replaceChildren();
    (d.intakes || []).forEach((r) => {
      const tr = el("tr");
      tr.append(el("td", "dim", String(r.id)));
      tr.append(el("td", null, r.target || "—"));
      tr.append(el("td", null, r.hospital || "—"));
      tr.append(el("td", null, r.date_value || "—"));
      const st = el("td");
      st.append(el("span", "badge " + (r.status === "긴급" ? "need"
        : r.status === "확정" ? "ok" : "guess"), r.status));
      tr.append(st);
      const act = el("td");
      if (r.status !== "확정" && r.status !== "긴급") {
        const b = el("button", null, "확정");
        b.onclick = () => confirmIntake(r);
        act.append(b);
      }
      tr.append(act);
      tb.append(tr);
    });
    if (!tb.children.length) tb.append(el("tr")).append(el("td", "dim", "접수 없음"));
  } catch (e) {
    box.replaceChildren(el("div", "bad", "불러오지 못했습니다: " + e.message));
  }
}

// 확정은 사람의 영역이다. 값을 그대로 확인시키고 누른 사람을 감사 로그에 남긴다.
async function confirmIntake(r) {
  const hospital = prompt("확정할 병원", r.hospital || "");
  if (hospital === null) return;
  const date = prompt("확정할 방문일 (YYYY-MM-DD)", r.date_value || "");
  if (date === null) return;
  const level = prompt("동행 지원 수준", r.need_level || "차량+동행");
  if (level === null) return;
  try {
    await api.confirmIntake(r.id, { hospital, date, level, actor: ACTOR, role: ROLE });
    loadQueue();
  } catch (e) {
    alert("확정 실패: " + e.message);
  }
}

// ── 접수카드 ───────────────────────────────────────────────
function renderField(f) {
  const box = el("div", "field");
  const head = el("div", "field-head");
  head.append(el("span", "field-label", f.label));
  head.append(el("span", "badge " + (STATUS_CLASS[f.status] || ""), f.status));
  box.append(head);
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

function renderCard(d) {
  const c = d.card, frag = document.createDocumentFragment();
  frag.append(el("div", "summary", c.summary));
  const meta = el("div", "meta");
  meta.append(el("span", null, `${c.target} (${c.phone_masked})`));
  meta.append(el("span", null, `${d.channel} · ${d.intent}`));
  meta.append(el("span", "src", `판정 근거: ${d.intent_source}`));
  frag.append(meta);
  frag.append(el("blockquote", null, `“${c.raw_utterance}”`));

  if (c.flags?.length) {
    const f = el("div", "flags");
    c.flags.forEach((x) => f.append(el("span", "flag", x)));
    frag.append(f);
  }

  const grid = el("div", "fields");
  Object.values(c.fields || {}).forEach((f) => grid.append(renderField(f)));
  frag.append(grid);

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

  const lists = [
    ["확인 질문 (콜백용)", c.confirm_questions],
    ["외출 전 참고", c.outing_checklist],
  ];
  lists.forEach(([title, items]) => {
    if (!items?.length) return;
    const b = el("div", "block");
    b.append(el("h3", null, title));
    const ul = el("ul", "questions");
    items.forEach((x) => ul.append(el("li", null, x)));
    b.append(ul);
    frag.append(b);
  });

  if (c.target_candidates?.length) {
    const b = el("div", "block");
    b.append(el("h3", null, `대상자 후보 ${c.target_candidates.length}명 — 확정 아님`));
    const ul = el("ul", "questions");
    c.target_candidates.forEach((x) =>
      ul.append(el("li", null, `${x.name} (${x.region}) — 보호자 ${x.guardian_name || "?"} ${x.guardian_relation || ""}`)));
    b.append(ul);
    frag.append(b);
  }

  if (c.reference_candidates?.length) {
    const b = el("div", "block");
    b.append(el("h3", null, "참고 후보 — 거리 기준, 확정 후보 아님"));
    const ul = el("ul", "questions");
    c.reference_candidates.forEach((h) =>
      ul.append(el("li", null, `${h.name} (${Math.round(h.distance_m)}m) — ${h.basis}`)));
    b.append(ul);
    frag.append(b);
  }

  if (d.facilities?.length) {
    const b = el("div", "block");
    b.append(el("h3", null, "지역 복지자원"));
    const ul = el("ul", "facilities");
    d.facilities.forEach((x) => {
      const li = el("li");
      li.append(el("span", "badge " + (x.region_match === "관내" ? "ok" : "guess"), x.region_match));
      li.append(el("span", null, ` ${x.name} — ${x.basis} [${x.source}]`));
      ul.append(li);
    });
    b.append(ul);
    frag.append(b);
  }

  if (d.intake_id != null) {
    frag.append(el("div", "small", `접수 번호 ${d.intake_id} — 확정은 '접수 대기' 탭에서`));
  }
  if (d.policy) {
    frag.append(el("div", "policy",
      `AI 범위: ${d.policy.ai_scope} · 의료 판단 ${d.policy.medical_judgement ? "함" : "안 함"}` +
      ` · 사람 검토 ${d.policy.human_review_required ? "필수" : "불필요"}`));
  }
  return frag;
}

function renderUrgent(d) {
  const w = el("div", "urgent " + (d.urgent_confident ? "hard" : "soft"));
  w.append(el("div", "urgent-title",
    d.urgent_confident ? "긴급 신호 감지 — 접수 중단" : "발화를 이해하지 못함 — 담당자 확인"));
  w.append(el("p", null, d.urgent_message || ""));
  return w;
}

async function submitIntake(fn) {
  const out = $("result");
  out.className = "loading";
  out.textContent = "분석 중…";
  try {
    const d = await fn();
    out.className = "";
    out.replaceChildren(d.urgent ? renderUrgent(d) : renderCard(d));
    if (d.stt) {
      out.prepend(el("div", "stt" + (d.stt.needs_review ? " warn" : ""),
        `STT: “${d.stt.text}”` + (d.stt.needs_review ? " — 확신도 낮음, 사람 확인 필요" : "")));
    }
    if (d.intake_id != null) $("prIntakeId").value = d.intake_id;
  } catch (e) {
    out.className = "bad";
    out.textContent = "오류: " + e.message;
  }
}

$("btnIntake").onclick = () =>
  submitIntake(() => api.createIntake($("phone").value, $("utterance").value, $("channel").value));
$("btnPick").onclick = () => $("audioFile").click();
$("audioFile").onchange = (e) => {
  const f = e.target.files[0];
  if (f) submitIntake(() => api.intakeFromAudio(f, $("phone").value, $("channel").value));
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
    submitIntake(() => api.intakeFromAudio(blob, $("phone").value, $("channel").value));
  };
  rec.start();
  btn.textContent = "■ 정지";
  hint.textContent = "녹음 중… 말한 뒤 정지를 누르세요";
};

// ── 사후기록 ───────────────────────────────────────────────
$("btnPost").onclick = async () => {
  const out = $("postResult");
  out.className = "loading";
  out.textContent = "초안 만드는 중…";
  try {
    const d = await api.createPostRecord(
      Number($("prIntakeId").value || 0), $("prPhone").value, $("prMemo").value,
      "정형외과", null);
    out.className = "";
    out.replaceChildren(renderDraft(d));
  } catch (e) {
    out.className = "bad";
    out.textContent = "오류: " + e.message;
  }
};

const DRAFT_LABELS = {
  treatment: "진료 내용", next_visit: "다음 진료", pharmacy: "약국",
  cautions: "다음 동행 주의사항", guardian_msg: "보호자 공유 메시지",
  profile_update: "케어 프로필 업데이트 제안",
};

function renderDraft(d) {
  const frag = document.createDocumentFragment();
  if (d.needs_schedule_check) {
    frag.append(el("div", "flags")).append(
      el("span", "flag", "일정 재확인 — 상대날짜는 확정하지 않았습니다"));
  }
  const dl = el("dl", "receipt");
  for (const [k, label] of Object.entries(DRAFT_LABELS)) {
    if (!d.draft[k]) continue;
    dl.append(el("dt", null, label));
    dl.append(el("dd", null, d.draft[k]));
  }
  frag.append(dl);

  // AI는 프로필을 자동 변경하지 않는다 — 승인 버튼을 눌러야 반영된다
  const row = el("div", "row");
  const ok = el("button", "primary", "승인 — 프로필에 반영");
  const no = el("button", null, "거절");
  const msg = el("span", "hint");
  const act = async (approved) => {
    ok.disabled = no.disabled = true;
    try {
      const r = await api.approvePostRecord(d.record_id, approved, ROLE);
      msg.textContent = !r.changed ? "이미 같은 상태입니다(중복 요청)."
        : approved ? (r.applied ? "승인 — 프로필에 반영했습니다." : "승인했습니다(반영할 제안 없음).")
        : "거절했습니다.";
    } catch (e) {
      msg.textContent = "실패: " + e.message;
    } finally {
      ok.disabled = no.disabled = false;
    }
  };
  ok.onclick = () => act(true);
  no.onclick = () => act(false);
  row.append(ok, no, msg);
  frag.append(row);
  frag.append(el("div", "small", `기록 번호 ${d.record_id} · 생성 근거: ${d.source}`));
  return frag;
}

// ── 감사 로그 ──────────────────────────────────────────────
async function loadAudit() {
  const tb = $("audit").querySelector("tbody");
  try {
    const rows = await api.audit(30);
    tb.replaceChildren();
    rows.forEach((r) => {
      const tr = el("tr");
      tr.append(el("td", "dim", (r.at || "").replace("T", " ").slice(0, 16)));
      tr.append(el("td", null, r.actor || "—"));
      tr.append(el("td", null, r.role || "—"));
      tr.append(el("td", null, r.action || "—"));
      tr.append(el("td", "dim", `${r.target_type || ""} ${r.target_id || ""}`));
      tr.append(el("td", "dim", r.detail || ""));
      tb.append(tr);
    });
    if (!tb.children.length) tb.append(el("tr")).append(el("td", "dim", "기록 없음"));
  } catch (e) {
    tb.replaceChildren();
    tb.append(el("tr")).append(el("td", "bad", "불러오지 못했습니다: " + e.message));
  }
}

loadStatus();
loadQueue();
