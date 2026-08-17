// 사후기록 — AI 초안을 사회복지사가 고쳐서 승인한다.
//
// **초안이 편집 가능한 것이 이 화면의 핵심이다.** 예전에는 승인/거절만 받아서,
// 초안이 조금 틀렸을 때 할 수 있는 일이 '거절' 뿐이었다 — 고쳐 쓰면 되는
// 기록까지 통째로 버려졌다. 지금은 고친 내용을 함께 보내고, 서버가 초안 원본과
// 비교해 몇 칸을 그대로 썼는지 감사 로그에 남긴다.
//
// 그 누적이 제출 문서(파일1) 4-2 의 '사후기록 초안 수정률'이 된다. 따로 재는
// 것이 아니라 승인 한 건이 곧 표본 한 건이다.

import { api } from "../../api.js";
import { badge, button, chips, dateLabel, el, empty, errorBox, listRow, sectionTitle } from "../ui.js";
import { can, reload, state, update } from "../app.js";

const FILTERS = [["전체", "전체"], ["기록 필요", "기록 필요"], ["기록 완료", "기록 완료"]];

// 초안 6칸. 서버의 POST_FIELDS 와 순서까지 맞춰 둔다.
const FIELDS = [
  ["treatment", "진료 내용"],
  ["next_visit", "다음 진료"],
  ["pharmacy", "약국"],
  ["cautions", "다음 동행 주의사항"],
  ["guardian_msg", "보호자 공유 메시지"],
  ["profile_update", "케어 프로필에 반영할 내용"],
];

export function renderRecords() {
  return el("div", "main", [listPane(), state.recordDraft ? writePane() : detailPane()]);
}

// ── 새 기록 쓰기 ────────────────────────────────────────────
//
// 동행이 끝나면 매니저가 음성으로 메모를 남기고, 그게 AI 초안이 된다.
// 목업에는 이미 만들어진 초안을 고치는 화면만 있어서 **메모를 넣는 입구가
// 없었다** — 시연 장면 5 가 이 경로다.
function writePane() {
  const memo = el("textarea");
  memo.rows = 5;
  memo.placeholder = "동행 매니저 메모 — 오늘 진료가 어땠는지 그대로 적거나 말하세요";
  memo.style.cssText = "width:100%;padding:9px 11px;border:1px solid var(--line);"
                     + "border-radius:8px;background:var(--white);resize:vertical";

  const intakeId = el("select");
  // 확정된 접수만 고를 수 있다 — 동행을 다녀왔어야 사후기록이 있다.
  const done = state.intakes.filter((r) => r.status === "확정");
  for (const r of done) {
    const o = el("option", null,
      `#${r.id} ${r.target} · ${r.confirmed_hospital || r.hospital || ""}`);
    o.value = r.id;
    intakeId.append(o);
  }
  intakeId.style.cssText = "padding:9px 11px;border:1px solid var(--line);"
                         + "border-radius:8px;background:var(--white);width:100%";

  const status = el("div", "ask", "");
  const go = button("초안 만들기", "btn primary", async () => {
    const text = memo.value.trim();
    if (!text) { status.textContent = "메모를 적어 주세요."; return; }
    if (!intakeId.value) { status.textContent = "확정된 접수가 없어요 — 먼저 확정해 주세요."; return; }
    go.disabled = true;
    status.textContent = "초안을 만드는 중…";
    try {
      const r = state.intakes.find((x) => String(x.id) === intakeId.value);
      const res = await api.createPostRecord(Number(intakeId.value), r.phone, text,
                                             r.dept || null, r.target || null);
      update({ recordDraft: false, selectedRecord: res.record_id });
      await reload();
    } catch (e) {
      status.textContent = "실패 — " + e.message;
      go.disabled = false;
    }
  });

  return el("div", "detail-pane", [
    el("div", "detail-head", [
      el("h1", null, "새 사후기록"),
      el("div", "right", [
        button("취소", "btn ghost", () => update({ recordDraft: false })),
      ]),
    ]),
    el("div", "cols", [el("div", "col", [
      sectionTitle("어느 동행인가요"),
      el("div", "frow", [el("div", "lb", "접수"), el("div", "vl", [intakeId])]),
      sectionTitle("매니저 메모", "말한 그대로 적으면 AI 가 항목별로 정리합니다"),
      el("div", "frow", [el("div", "lb", "메모"), el("div", "vl", [memo])]),
      el("div", "footbar", [status, el("div", "grow"), go]),
    ])]),
  ]);
}

function listPane() {
  const rows = filtered();
  return el("div", "list-pane", [
    el("div", "pane-head", [
      el("h2", null, [document.createTextNode("사후기록"),
                      el("span", "sub", `기록 필요 ${pending().length}건`)]),
      can("post.write") || can("post.approve")
        ? button("+ 새 기록 쓰기", "btn sm",
                 () => update({ recordDraft: true, selectedRecord: null }))
        : null,
    ]),
    chips(FILTERS, state.recordFilter, (k) => update({ recordFilter: k })),
    el("div", "pane-scroll",
      rows.length ? rows.map(rowOf) : [empty("이 조건의 기록이 없어요.")]),
  ]);
}

const pending = () => state.records.filter((r) => !r.approved);

function filtered() {
  if (state.recordFilter === "기록 필요") return pending();
  if (state.recordFilter === "기록 완료") return state.records.filter((r) => r.approved);
  return state.records;
}

function rowOf(r) {
  return listRow({
    name: `기록 #${r.id}`,
    right: badge(r.approved ? "기록 완료" : "기록 필요", r.approved ? "ok" : "need"),
    sub: r.treatment || "(초안 없음)",
    meta: [r.created_at && dateLabel(r.created_at.slice(0, 10)), `접수 #${r.intake_id}`]
      .filter(Boolean).join(" · "),
    selected: state.selectedRecord === r.id,
    onClick: () => update({ selectedRecord: r.id }),
  });
}

function detailPane() {
  if (state.error) return el("div", "wide-pane", [errorBox(state.error)]);
  const r = state.records.find((x) => x.id === state.selectedRecord);
  if (!r) return el("div", "detail-pane", [empty("왼쪽에서 기록을 고르면 초안이 열려요.")]);

  // 입력칸을 매 렌더마다 새로 만들되, 값은 서버가 준 것으로 채운다.
  // 편집 중인 값을 state 에 두지 않는 이유는, 폴링이 이 화면에서는 돌지
  // 않기 때문이다(app.js pollable 참고) — 타이핑 중에 덮어써질 일이 없다.
  const inputs = {};
  const rows = FIELDS.map(([key, label]) => {
    const ta = el("textarea");
    ta.value = r[key] || "";
    ta.rows = key === "treatment" || key === "guardian_msg" ? 3 : 2;
    ta.style.cssText = "width:100%;padding:9px 11px;border:1px solid var(--line);"
                     + "border-radius:8px;background:var(--white);resize:vertical";
    ta.disabled = !!r.approved || !can("post.approve");
    inputs[key] = ta;
    return el("div", "frow", [
      el("div", "lb", label),
      el("div", "vl", [ta]),
    ]);
  });

  return el("div", "detail-pane", [
    el("div", "detail-head", [
      el("h1", null, `사후기록 #${r.id}`),
      badge(r.approved ? "기록 완료" : "기록 필요", r.approved ? "ok" : "need"),
      el("div", "right", `접수 #${r.intake_id}`),
    ]),
    el("div", "cols", [el("div", "col", [
      sectionTitle("동행 매니저 메모", "음성으로 받은 원문"),
      el("div", "quote", [
        el("div", "who", "매니저"),
        el("div", "txt", `“${r.memo_raw || "—"}”`),
      ]),
      sectionTitle("AI 초안", "그대로 쓰거나 고쳐서 승인하세요"),
      ...rows,
      footer(r, inputs),
    ])]),
  ]);
}

function footer(r, inputs) {
  if (r.approved) {
    return el("div", "footbar", [
      el("div", "msg", "승인된 기록이에요. 케어 프로필에 반영됐습니다."),
      el("div", "grow"),
    ]);
  }
  if (!can("post.approve")) {
    return el("div", "footbar", [
      el("div", "msg", "승인 권한이 없어요 — 사회복지사가 승인합니다."),
      el("div", "grow"),
    ]);
  }

  const go = button("승인하고 프로필에 반영", "btn primary", async () => {
    go.disabled = true;
    // 고친 칸만 보낸다. 안 보낸 칸은 서버가 초안 그대로 둔다 —
    // 빈 문자열과 '안 보냄'은 다르다.
    const edits = {};
    for (const [key] of FIELDS) {
      const v = inputs[key].value;
      if (v !== (r[key] || "")) edits[key] = v;
    }
    try {
      await api.approvePostRecord(r.id, true, Object.keys(edits).length ? edits : null);
      await reload();
    } catch (e) {
      update({ error: e });
    }
  });

  return el("div", "footbar", [
    el("div", "msg", ""),
    el("div", "grow"),
    button("반영하지 않음", "btn", async () => {
      try { await api.approvePostRecord(r.id, false); await reload(); }
      catch (e) { update({ error: e }); }
    }),
    go,
  ]);
}
