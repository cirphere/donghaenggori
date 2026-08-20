// 사회복지사 콘솔 — 접수 확인·확정, 사후기록 승인, 감사 로그.
//
// 보호자 웹과 나눈 이유는 화면 구성이 아니라 **권한**이다. 여기서만 확정·승인·
// 감사로그 조회를 한다.
//
// 신원은 **서버가 정한다.** 예전엔 이 파일에 ROLE/ACTOR 를 상수로 박아 요청
// 본문에 실어 보냈는데, 그러면 화면이 말하는 사람과 감사 로그에 남는 사람이
// 달라진다. 지금은 로그인 토큰에서 서버가 꺼내 쓰고, 화면은 /api/auth/me 가
// 준 이름을 그대로 보여주기만 한다.

import { api, session } from "../api.js";

// 서버가 로그인 응답에 실어 준 권한 목록으로 화면을 가린다.
//
// 역할 이름을 여기 하드코딩하지 않는다 — `role === "사회복지사"` 를 JS 에 두면
// 권한표가 두 곳이 되고, 역할이 하나 늘 때 한쪽만 고치면 어긋난다.
//
// **가리는 것은 안내일 뿐 경계가 아니다.** 실제 차단은 서버의 403 이다.
// 동행매니저가 확정 버튼을 못 보게 하는 이유는 권한 때문이 아니라, 눌러서
// 값을 다 채운 뒤에야 실패하는 경험을 없애려는 것이다.
const can = (perm) => (session.user?.permissions || []).includes(perm);

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const STATUS_CLASS = { "확인됨": "ok", "추정": "guess", "확인 필요": "need" };

// Node.append() 는 undefined 를 돌려주므로 체이닝하면 안 된다
const rowOf = (td) => { const tr = el("tr"); td.colSpan = 6; tr.append(td); return tr; };

// ── 탭 ─────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".view").forEach((x) => x.classList.add("hidden"));
    t.classList.add("active");
    $("view-" + t.dataset.view).classList.remove("hidden");
    closeModal();
    if (t.dataset.view === "queue") loadQueue();
    if (t.dataset.view === "audit") loadAudit();
  };
});

// ── 자동 갱신 ──────────────────────────────────────────────
//
// 전화로 들어온 접수는 화면이 아니라 서버에서 생긴다. 갱신이 없으면 복지사가
// 탭을 다시 누를 때까지 새 접수가 안 보인다 — 전화가 입구인 서비스에서 그건
// 접수를 놓치는 것과 같다.
//
// 도는 조건을 좁게 잡는다. 접수 대기 화면이 실제로 보이고, 모달이 없고,
// 브라우저 탭이 앞에 있을 때만. 목록이 새로 그려지는 것은 그 화면을 보고
// 있는 사람에게만 의미가 있고, 나머지는 서버를 찌르기만 한다.
const POLL_MS = 3000;
let pollTimer = null;

function pollable() {
  return !$("view-queue").classList.contains("hidden")
    && !document.querySelector(".modal-backdrop")
    && document.visibilityState === "visible";
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => { if (pollable()) loadQueue(); }, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

// 탭을 뒤로 보냈다 돌아오면 그 사이 쌓인 것을 즉시 반영한다. 3초를 더
// 기다리게 하면 "안 도는 것"처럼 보인다.
document.addEventListener("visibilitychange", () => {
  if (pollable()) loadQueue();
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
let queueInFlight = false;

async function loadQueue() {
  // 3초마다 도는데 응답이 그보다 느리면 요청이 겹쳐 쌓인다. 먼저 온 응답이
  // 나중 것을 덮어써 목록이 과거로 되돌아가는 것도 이 때문이다.
  if (queueInFlight) return;
  queueInFlight = true;
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
      // 긴급 전환 결과. 담당자가 못 받은 건은 반드시 눈에 띄어야 한다.
      if (r.transfer_status) {
        const okConn = r.transfer_status === "연결됨";
        st.append(document.createTextNode(" "));
        st.append(el("span", "badge " + (okConn ? "ok" : "need"),
                     "전환 " + r.transfer_status));
      }
      // 전화 2턴에서 받은 본인 확인 답변. AI가 확정한 것이 아니라 들은 말이다.
      if (r.identity_answer || r.identity_status) {
        const b = el("span", "badge " + (r.identity_status === "추정" ? "guess" : "need"),
                     "본인확인 " + (r.identity_status || "확인 필요"));
        b.title = r.identity_answer ? `통화 답변: “${r.identity_answer}”` : "답변 없음";
        st.append(document.createTextNode(" "));
        st.append(b);
      }
      tr.append(st);
      const act = el("td", "actions");
      // 상세보기 — 팝업으로 요약·본인확인·확정결과·카드를 한 번에 본다
      const detail = el("button", null, "상세보기");
      detail.onclick = () => openDetail(r.id);
      act.append(detail);
      if (r.status === "긴급") {
        // 긴급은 접수카드가 없어 확정할 대상이 없다. 사람이 연락을 끝냈다는
        // 표시만 한다 — 안 그러면 목록 맨 위에 쌓여 새 긴급이 묻힌다.
        if (can("intake.confirm")) {
          const b = el("button", null, "처리 완료");
          b.onclick = () => resolveUrgent(r);
          act.append(b);
        }
      } else if (r.status !== "확정" && r.status !== "긴급 처리됨" && can("intake.confirm")) {
        const b = el("button", null, "확정");
        b.onclick = () => confirmIntake(r);
        act.append(b);
      }
      tr.append(act);
      // 행을 그냥 누르면 한 단계 들어가 카드만 크게 본다.
      // 팝업은 훑어보는 용도, 이쪽은 들여다보는 용도다.
      tr.classList.add("clickable");
      tr.onclick = (e) => {
        if (e.target.tagName === "BUTTON") return;   // 버튼 클릭과 겹치지 않게
        showCardView(r.id);
      };
      tb.append(tr);
    });
    if (!tb.children.length) tb.append(rowOf(el("td", "dim", "접수 없음")));
  } catch (e) {
    box.replaceChildren(el("div", "bad", "불러오지 못했습니다: " + e.message));
  } finally {
    queueInFlight = false;
  }
}

// 확정은 사람의 영역이다. 값을 그대로 확인시키고 누른 사람을 감사 로그에 남긴다.
//
// 확정 상세를 먼저 받아오는 이유: 무엇이 막고 있는지를 **묻기 전에** 알아야 한다.
// 예전에는 병원·방문일을 prompt 로 다 받아낸 뒤 서버가 409 로 막았는데, 복지사가
// 병원명을 손으로 적은 다음에 "병원이 확인되지 않았습니다" 를 보는 순서가 됐다.
// 같은 정보를 두 번, 그것도 거꾸로 물은 셈이다.
async function confirmIntake(r) {
  try {
    showConfirm(r, await api.getIntake(r.id));
  } catch (e) {
    alert("접수를 불러오지 못했습니다: " + e.message);
  }
}

// 확정 화면 — 팝업 하나로 끝낸다.
//
//   위   먼저 확인할 내용 (막힌 항목이 있을 때만) — 되물을 질문 + 답 적는 칸
//   아래 확정할 내용 — 병원·방문일·지원수준. 확인된 값이 이미 채워져 있다.
//
// draft 는 다시 그려도 살아남는 입력값이다. 항목 하나를 확인 저장하면 화면을
// 새로 그리는데, 그때 복지사가 아래쪽에 적어 둔 값이 날아가면 안 된다.
function showConfirm(r, d, draft) {
  const c = d.card || {};
  const gate = d.gate || { allowed: true, blockers: [] };
  const fields = c.fields || {};
  draft = draft || {
    hospital: c.hospital || d.hospital || "",
    date: c.date_value || d.date_value || "",
    level: c.need_level || d.need_level || "차량+동행",
  };

  const body = openModal(`접수 확정 — ${d.target || r.target || ""}`);
  const inputs = {};
  const readForm = () => {
    for (const [k, node] of Object.entries(inputs)) draft[k] = node.value.trim();
  };

  // ── 먼저 확인할 내용 ────────────────────────────────────
  if (gate.blockers.length) {
    const block = el("div", "block");
    block.append(el("h3", null, `먼저 확인할 내용 ${gate.blockers.length}건`));
    block.append(el("div", "small", gate.hard_block
      ? "기관 규칙상 확인 필요가 남으면 확정할 수 없습니다."
      : "확인하지 않고 접수할 수도 있습니다. 그 사실은 감사 로그에 남습니다."));
    gate.blockers.forEach((b) => block.append(blockerBox(r, b, d, draft, readForm)));
    body.append(block);
  }

  // ── 확정할 내용 ────────────────────────────────────────
  const form = el("div", "block");
  form.append(el("h3", null, "확정할 내용"));
  [["hospital", "병원", "hospital"], ["date", "방문일 (YYYY-MM-DD)", "date"],
   ["level", "동행 지원 수준", null]].forEach(([key, label, fieldName]) => {
    const box = el("div", "field");
    const head = el("div", "field-head");
    head.append(el("span", "field-label", label));
    // AI 가 낸 값인지 사람이 확인한 값인지 여기서도 보여야 한다
    const st = fieldName && fields[fieldName] && fields[fieldName].status;
    if (st) head.append(el("span", "badge " + (STATUS_CLASS[st] || ""), st));
    box.append(head);
    const input = el("input");
    input.value = draft[key] || "";
    inputs[key] = input;
    box.append(input);
    form.append(box);
  });
  body.append(form);

  // ── 버튼 ───────────────────────────────────────────────
  const foot = el("div", "modal-foot");
  const cancel = el("button", null, "취소");
  cancel.onclick = closeModal;
  foot.append(cancel);
  if (!gate.blockers.length) {
    const go = el("button", "primary", "확정");
    go.onclick = () => { readForm(); sendConfirm(r, { ...draft }); };
    foot.append(go);
  } else if (!gate.hard_block) {
    // 확인 없이 넘어갈 때는 이유를 함께 받는다. 사고가 났을 때 "연락이 닿지
    // 않았다"와 "물어볼 필요 없다고 봤다"는 책임이 전혀 다른데, 감사 로그에
    // '미확인 확정'만 남으면 그 둘을 구분할 수 없다.
    const why = el("select", "ack-reason");
    for (const [v, t] of [["", "넘어가는 이유를 고르세요"],
                          ["연락이 닿지 않음", "연락이 닿지 않음"],
                          ["이미 알고 있음", "이미 알고 있음"],
                          ["물어볼 필요 없음", "물어볼 필요 없음"],
                          ["기타", "기타"]]) {
      const o = el("option", null, t);
      o.value = v;
      why.append(o);
    }
    foot.append(why);

    const go = el("button", "danger", "이대로 접수");
    go.disabled = true;
    why.onchange = () => { go.disabled = !why.value; };
    go.onclick = () => {
      readForm();
      sendConfirm(r, { ...draft }, true, why.value);
    };
    foot.append(go);
  }
  body.append(foot);
}

// 막힌 항목 하나 — 되물을 질문과 답 적는 칸.
//
// 질문을 함께 띄우는 게 핵심이다. 사회복지사가 이 화면을 띄운 채로 어르신께
// 전화를 걸어 그대로 물어보고 답을 바로 적는 자리다.
function blockerBox(r, b, d, draft, readForm) {
  const box = el("div", "field");
  const head = el("div", "field-head");
  head.append(el("span", "field-label", b.label));
  head.append(el("span", "badge need", "확인 필요"));
  box.append(head);

  // 어르신이 말한 표현·통화에서 받아 적은 성함은 되물을 때 그대로 쓴다
  const heard = (b.heard || []).map((h) => `${h.label} “${h.value}”`).join(" · ");
  const said = b.spoken ? `어르신 말씀: “${b.spoken}”` : "";
  if (said || heard) box.append(el("div", "small", [said, heard].filter(Boolean).join(" · ")));
  if (b.question) box.append(el("div", "qa", b.question));

  const row = el("div", "verify-row");
  const input = el("input");
  input.placeholder = "통화로 확인한 값";
  // 대상자의 value 는 "신규 대상자(미등록 번호)" 같은 **표시 문자열**이지 이름이
  // 아니다. 그대로 채워 두면 손대지 않고 저장했을 때 대상자 이름이 그 문장이
  // 된다. 통화에서 받아 적은 성함이 있으면 그게 확인할 값이다.
  const heardName = (b.heard || []).find((h) => (h.label || "").includes("성함"));
  input.value = (b.field === "target" ? (heardName && heardName.value) : b.value) || "";
  const save = el("button", null, "확인 완료로 저장");
  save.onclick = async () => {
    const value = input.value.trim();
    if (!value) return;
    readForm();                       // 아래쪽에 적어 둔 값을 먼저 챙긴다
    save.disabled = true;
    try {
      const res = await api.verifyField(r.id, b.field, value);
      // 확인한 값이 확정값이다. 이걸 안 옮기면 확인 전 값으로 확정된다.
      if (b.field === "hospital") draft.hospital = value;
      if (b.field === "date") draft.date = value;
      showConfirm(r, res.intake, draft);
    } catch (e) {
      save.disabled = false;
      alert("확인 저장 실패: " + e.message);
    }
  };
  row.append(input, save);
  box.append(row);
  return box;
}

async function sendConfirm(r, payload, acknowledge = false, acknowledgeReason = null) {
  try {
    await api.confirmIntake(r.id, { ...payload, acknowledge, acknowledgeReason });
    closeModal();
    loadQueue();
  } catch (e) {
    // 409 는 요청이 틀린 게 아니라 지금 상태에서 확정할 수 없다는 뜻이다.
    // 화면을 열 때 이미 게이트를 확인했으므로 여기 걸리는 건 그 사이에 상태가
    // 바뀐 경우다 — 최신 상태로 다시 그린다.
    if (e.status === 409) {
      api.getIntake(r.id)
        .then((fresh) => showConfirm(r, fresh, payload))
        .catch(() => alert("확정 실패: " + e.message));
      return;
    }
    alert("확정 실패: " + e.message);
  }
}

// ── 팝업 ───────────────────────────────────────────────────
// 접수 목록 위에 겹쳐 띄운다. 목록을 떠나지 않고 훑어볼 수 있게 하려는 것이다.
function openModal(title) {
  closeModal();
  const back = el("div", "modal-backdrop");
  const box = el("div", "modal");
  const head = el("div", "modal-head");
  head.append(el("strong", null, title));
  const x = el("button", "modal-x", "닫기");
  x.onclick = closeModal;
  head.append(x);
  const body = el("div", "modal-body");
  box.append(head, body);
  back.append(box);
  // 바깥을 누르면 닫는다. 안쪽 클릭은 안 닫히게 타깃을 확인한다.
  back.onclick = (e) => { if (e.target === back) closeModal(); };
  document.body.append(back);
  document.addEventListener("keydown", onEsc);
  return body;
}

function closeModal() {
  document.querySelectorAll(".modal-backdrop").forEach((x) => x.remove());
  document.removeEventListener("keydown", onEsc);
}

function onEsc(e) { if (e.key === "Escape") closeModal(); }

async function openDetail(id) {
  const body = openModal(`접수 #${id}`);
  body.append(el("div", "dim", "불러오는 중…"));
  try {
    const d = await api.getIntake(id);
    body.replaceChildren(renderStored(d));
  } catch (e) {
    body.replaceChildren(el("div", "bad", "불러오지 못했습니다: " + e.message));
  }
}

// ── 카드 화면 ──────────────────────────────────────────────
// 행을 누르면 목록을 접고 카드만 크게 띄운다. 확인 전화를 걸면서 볼 화면이라
// 본인확인·확정결과 같은 관리 정보는 빼고 카드만 남긴다.
async function showCardView(id) {
  const view = $("view-card"), body = $("cardBody");
  document.querySelectorAll(".view").forEach((x) => x.classList.add("hidden"));
  view.classList.remove("hidden");
  $("cardTitle").textContent = `접수 #${id}`;
  body.replaceChildren(el("div", "dim", "불러오는 중…"));
  try {
    const d = await api.getIntake(id);
    body.replaceChildren(d.card
      ? renderCard({ card: d.card, channel: d.channel, intent: d.intent,
                     intent_source: "저장된 접수", facilities: [] })
      : noCardNotice(d));
    // 카드를 열자마자 확정 가능 여부가 보여야 한다. 상태 배지를 항목마다 세어
    // 보게 하면 결국 아무도 안 센다.
    if (d.gate && !d.gate.allowed && d.confirmed !== 1) {
      const labels = d.gate.blockers.map((b) => b.label).join(" · ");
      body.prepend(el("div", "notice",
        `확정하려면 ${d.gate.blockers.length}건을 먼저 확인해야 합니다 — ${labels}`));
    }
  } catch (e) {
    body.replaceChildren(el("div", "bad", "불러오지 못했습니다: " + e.message));
  }
}

function backToQueue() {
  document.querySelectorAll(".view").forEach((x) => x.classList.add("hidden"));
  $("view-queue").classList.remove("hidden");
}

// 긴급은 카드를 만들지 않는다. 이 기능 이전에 들어온 접수도 카드가 없다.
function noCardNotice(d) {
  const b = el("div", "block");
  b.append(el("div", "dim",
    d.status === "긴급" || d.status === "긴급 처리됨"
      ? "긴급 접수는 접수카드를 만들지 않습니다 — 사람에게 바로 넘긴 건입니다."
      : "이 접수에는 저장된 카드가 없습니다(카드 보존 기능 이전 접수)."));
  b.append(el("blockquote", null, `“${d.raw_utterance || ""}”`));
  return b;
}

// 저장된 접수 전체 — 팝업에서 쓴다. 카드에 더해 전화 본인확인과 확정 결과를
// 함께 보여준다.
function renderStored(d) {
  const frag = document.createDocumentFragment();

  if (d.transfer_status) {
    const b = el("div", "block");
    b.append(el("h3", null, "긴급 전환 결과"));
    const line = el("div", "need");
    line.append(el("span", "badge " + (d.transfer_status === "연결됨" ? "ok" : "need"),
                   d.transfer_status));
    if (d.transfer_status !== "연결됨") {
      line.append(el("span", null, "담당자와 연결되지 않았습니다 — 다시 연락 필요"));
    }
    b.append(line);
    frag.append(b);
  }

  if (d.identity_answer || d.identity_status) {
    const b = el("div", "block");
    b.append(el("h3", null, "전화 본인 확인"));
    const line = el("div", "need");
    line.append(el("span", "badge " + (d.identity_status === "추정" ? "guess" : "need"),
                   d.identity_status || "확인 필요"));
    line.append(el("span", null, d.identity_answer ? `“${d.identity_answer}”` : "답변 없음"));
    b.append(line);
    b.append(el("div", "small", "통화에서 들은 말 그대로입니다. AI가 확정한 것이 아닙니다."));
    frag.append(b);
  }

  if (d.confirmed) {
    const b = el("div", "block");
    b.append(el("h3", null, "확정 결과 (사회복지사)"));
    const dl = el("dl", "receipt");
    [["병원", d.confirmed_hospital], ["방문일", d.confirmed_date],
     ["지원 수준", d.confirmed_level]].forEach(([k, v]) => {
      dl.append(el("dt", null, k));
      dl.append(el("dd", null, v || "—"));
    });
    b.append(dl);
    frag.append(b);
  }

  frag.append(d.card
    ? renderCard({ card: d.card, channel: d.channel, intent: d.intent,
                   intent_source: "저장된 접수", facilities: [] })
    : noCardNotice(d));
  return frag;
}

async function resolveUrgent(r) {
  const note = prompt("어떻게 처리하셨나요? (감사 로그에 남습니다)", "담당자 통화 완료");
  if (note === null) return;
  try {
    await api.resolveUrgent(r.id, note);
    loadQueue();
  } catch (e) {
    alert("처리 실패: " + e.message);
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

  // **통화 흐름 그대로 원문 바로 아래에 붙인다.** 어르신이 처음 말한 것과
  // 우리가 되물어 받은 답은 같은 한 통화이고, 복지사는 그 순서로 읽는다.
  // 카드 아래쪽(동행 지원 수준 뒤)에 두었더니 통화 내용과 떨어져서, 값이
  // 어디서 왔는지 확인하려면 화면을 한참 내려야 했다.
  //
  // 후속답변은 **별도 녹음**이라 원문(raw_utterance)에 들어 있지 않다. 이
  // 칸이 없으면 복지사가 통화 과정을 확인할 방법이 아예 없다.
  if (c.followups?.length) {
    const b = el("div", "block talk");
    b.append(el("h3", null, "이어서 되물은 것 (통화 중)"));
    c.followups.forEach((f, i) => {
      const row = el("div", "followup");
      const q = el("div", "fq");
      q.append(el("span", "who", `후속질문 ${i + 1}`));
      q.append(el("span", null, f.question));
      row.append(q);
      // 답이 없는 경우가 실제로 있다(무응답·전사 실패). 빈칸으로 두면
      // "안 물어봤다" 와 "물었는데 답을 못 얻었다" 가 구분되지 않는다.
      const aRow = el("div", "fa");
      aRow.append(el("span", "who", "답변"));
      aRow.append(el("span", null, f.answer || "(답변 없음)"));
      row.append(aRow);
      // 결과가 없으면 그 항목은 아직 '확인 필요' 다 — 복지사가 다시 물어야 한다.
      // 색은 서버가 준 status 로 고른다. result 문자열을 파싱하면 문구를 다듬는
      // 순간 깨지고, 그건 화면이 조용히 틀리는 종류의 버그다.
      const meta = el("div", "fmeta");
      meta.append(el("span", "badge " + (STATUS_CLASS[f.status] || "need"),
        f.result ? `반영: ${f.result}` : "확인 필요 유지 — 사회복지사 확인"));
      if (f.at) meta.append(el("span", "src", f.at));
      row.append(meta);
      b.append(row);
    });
    // 되묻기를 중간에 그만둔 이유(어르신이 사람을 찾았거나 상한에 걸렸거나).
    // 이유를 안 보여주면 "왜 하나만 물었나" 에 답할 수 없다. 통화의 마지막
    // 장면이라 여기 붙는 것이 맞다.
    if (c.followup_stopped) b.append(el("div", "stopped", c.followup_stopped));
    frag.append(b);
  } else if (c.followup_stopped) {
    // 한 번도 못 묻고 끝난 경우 — 이유만 남는다.
    const b = el("div", "block talk");
    b.append(el("h3", null, "되묻기를 그만둔 이유"));
    b.append(el("div", "stopped", c.followup_stopped));
    frag.append(b);
  }

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
    // append() 는 undefined 를 돌려준다 — 체이닝하면 그 자리에서 죽는다.
    // (파일 맨 위에 적어 둔 규칙인데 여기서 어기고 있었다. "2주 뒤" 처럼 상대
    //  날짜가 든 메모에서만 터져서 오래 안 보였다.)
    const flags = el("div", "flags");
    flags.append(el("span", "flag", "일정 재확인 — 상대날짜는 확정하지 않았습니다"));
    frag.append(flags);
  }
  const dl = el("dl", "receipt");
  for (const [k, label] of Object.entries(DRAFT_LABELS)) {
    if (!d.draft[k]) continue;
    dl.append(el("dt", null, label));
    dl.append(el("dd", null, d.draft[k]));
  }
  frag.append(dl);

  // AI는 프로필을 자동 변경하지 않는다 — 승인 버튼을 눌러야 반영된다.
  //
  // 다만 승인은 사회복지사·관리자만 할 수 있다(post.approve). 동행매니저는
  // 기록을 **쓰는** 사람이라, 자기가 쓴 초안을 자기가 승인하면 "사람이 검토한다"
  // 는 말이 무의미해진다. 버튼 대신 안내를 둔다.
  if (!can("post.approve")) {
    frag.append(el("p", "hint",
      "초안이 저장되었습니다. 프로필 반영은 사회복지사가 확인 후 승인합니다."));
    return frag;
  }

  const row = el("div", "row");
  const ok = el("button", "primary", "승인 — 프로필에 반영");
  const no = el("button", null, "거절");
  const msg = el("span", "hint");
  const act = async (approved) => {
    ok.disabled = no.disabled = true;
    try {
      const r = await api.approvePostRecord(d.record_id, approved);
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
    if (!tb.children.length) tb.append(rowOf(el("td", "dim", "기록 없음")));
  } catch (e) {
    tb.replaceChildren();
    tb.append(rowOf(el("td", "bad", "불러오지 못했습니다: " + e.message)));
  }
}

$("btnBack").onclick = backToQueue;

// ── 로그인 ─────────────────────────────────────────────────
//
// 화면을 두 덩어리로 나눠 두고(#view-login / #app) 통째로 바꾼다. 탭마다
// 로그인 여부를 확인하는 방식은 한 군데만 빠뜨려도 401 이 새어 나온다.

// 못 쓰는 탭은 아예 감춘다. 눌러서 "불러오지 못했습니다: 403" 을 보는 것보다,
// 애초에 없는 편이 화면을 믿게 만든다.
function applyPermissions() {
  const rules = [["audit", "audit.view"]];
  for (const [view, perm] of rules) {
    const ok = can(perm);
    document.querySelector(`.tab[data-view="${view}"]`)?.classList.toggle("hidden", !ok);
    // 감춘 탭이 열려 있던 채로 로그아웃·재로그인하면 빈 화면이 남는다
    if (!ok) $("view-" + view)?.classList.add("hidden");
  }
  // 감춘 탭이 활성 상태면 첫 탭으로 되돌린다
  if (document.querySelector(".tab.active")?.classList.contains("hidden")) {
    document.querySelector('.tab[data-view="queue"]')?.click();
  }
}

function showLogin(message) {
  // 세션이 끊겨서 여기로 온 것일 수 있다. 폴링을 세우지 않으면 3초마다
  // 401 을 받아 로그인 화면을 계속 다시 그린다.
  stopPolling();
  $("view-login").classList.remove("hidden");
  $("app").classList.add("hidden");
  $("whoami").textContent = "";
  $("btnLogout").classList.add("hidden");
  const err = $("loginError");
  if (message) { err.textContent = message; err.classList.remove("hidden"); }
  else { err.classList.add("hidden"); }
  $("loginPassword").value = "";
}

function showApp(user) {
  $("view-login").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("whoami").textContent = user ? `${user.name} (${user.role})` : "";
  $("btnLogout").classList.remove("hidden");
  applyPermissions();
  loadStatus();
  loadQueue();
  startPolling();
}

// 세션이 끊기면(만료·서버 재시작·/api/reset) 어느 요청에서든 여기로 돌아온다.
// api.js 가 401 을 받으면 토큰을 지우고 이걸 부른다.
session.onUnauthorized(() => showLogin("세션이 만료되었습니다. 다시 로그인해 주세요."));

async function doLogin() {
  const userId = $("loginId").value.trim();
  const password = $("loginPassword").value;
  if (!userId || !password) return showLogin("아이디와 비밀번호를 입력해 주세요.");
  $("btnLogin").disabled = true;
  try {
    const d = await api.login(userId, password);
    session.save(d.token, d.user);
    showApp(d.user);
  } catch (e) {
    showLogin(e.message);
  } finally {
    $("btnLogin").disabled = false;
  }
}

$("btnLogin").onclick = doLogin;
$("loginPassword").onkeydown = (e) => { if (e.key === "Enter") doLogin(); };
$("btnLogout").onclick = async () => {
  // 서버 세션도 지운다. 실패해도 화면은 로그아웃시킨다 — 토큰을 들고 있는
  // 것보다 낫다.
  try { await api.logout(); } catch { /* 이미 만료됐을 수 있다 */ }
  session.clear();
  showLogin();
};

// 새로고침해도 로그인이 유지되게 — 다만 토큰이 살아 있는지는 서버에 물어본다.
// sessionStorage 에 남아 있다고 유효한 것은 아니다(서버 재시작·만료).
(async () => {
  if (!session.token) return showLogin();
  try {
    showApp(await api.me());
  } catch {
    showLogin();          // 401 이면 api.js 가 이미 토큰을 지웠다
  }
})();
