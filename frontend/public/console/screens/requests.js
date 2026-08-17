// 요청 — 접수 목록과 접수카드 상세. 콘솔에서 제일 중요한 화면이다.
//
// 목업의 3단 구성을 그대로 옮겼다: 사이드바 | 요청 목록 | 접수카드.
// 카드는 다시 두 단으로 나뉜다 — 왼쪽은 **어르신이 무엇을 말했나**(원문·확인
// 과정), 오른쪽은 **그래서 무엇을 하기로 했나**(방문 정보·이동 지원·보호자).
//
// 이 순서가 중요하다. 근거를 먼저 보여주고 결론을 나중에 둔다. 반대로 하면
// 복지사가 AI 의 결론을 먼저 읽고 근거는 확인하지 않게 된다.

import { api } from "../../api.js";
import {
  badge, button, chips, dateLabelRelative, el, empty, errorBox, frow, listRow, sectionTitle,
} from "../ui.js";
import { can, openIntake, reload, state, update } from "../app.js";

const FILTERS = [["전체", "전체"], ["확인 필요", "확인 필요"], ["확정됨", "확정됨"]];

// 손이 더 가야 하는 접수 — 네비 배지·홈·목록이 **전부 이 함수 하나**를 쓴다.
//
// 예전엔 네비가 dashboard.counts.waiting 을, 목록이 intakes 를 봤다. waiting 은
// status='접수 대기' 만 세기 때문에 **임시 접수(미등록 번호)와 긴급이 빠졌고**,
// 그래서 배지 숫자와 실제 목록 건수가 달랐다. 둘 다 손봐야 하는 건들이다.
//
// 기준은 하나다 — 사회복지사가 아직 무언가 해야 하는가.
const DONE = ["확정", "긴급 처리됨"];
export const pendingIntakes = (intakes) =>
  (intakes || []).filter((r) => !DONE.includes(r.status));

export function renderRequests() {
  return el("div", "main", [listPane(), detailPane()]);
}

// ── 왼쪽: 목록 ──────────────────────────────────────────────
function listPane() {
  const rows = filtered();
  const n = pendingIntakes(state.intakes).length;
  return el("div", "list-pane", [
    el("div", "pane-head", [
      el("h2", null, [
        document.createTextNode("요청"),
        // 전체 건수와 손봐야 할 건수를 함께 적는다. 하나만 적으면 네비 배지와
        // 다른 숫자가 되어 어느 쪽이 맞는지 매번 세어 봐야 한다.
        el("span", "sub", `${state.intakes.length}건 · 확인 필요 ${n}`),
      ]),
    ]),
    chips(FILTERS, state.requestFilter, (k) => update({ requestFilter: k })),
    el("div", "pane-scroll",
      rows.length ? rows.map(rowOf) : [empty("이 조건의 요청이 없어요.")]),
  ]);
}

function filtered() {
  const f = state.requestFilter;
  if (f === "확정됨") return state.intakes.filter((r) => r.status === "확정");
  if (f === "확인 필요") return pendingIntakes(state.intakes);
  return state.intakes;
}

/** "박순자 (보호자 대리 요청 — 확인 필요)" → ["박순자", "보호자 대리 요청 — 확인 필요"] */
export function splitTarget(target) {
  const t = target || "미확인 대상자";
  const m = t.match(/^(.*?)\s*\((.+)\)\s*$/);
  return m ? [m[1], m[2]] : [t, null];
}

function rowOf(r) {
  const urgent = r.status === "긴급";
  const [name, note] = splitTarget(r.target);
  return listRow({
    name: note ? `${name} · ${note}` : name,
    right: badge(r.status, urgent ? "urgent" : r.status === "확정" ? "ok" : "need"),
    sub: [r.hospital, r.date_value && dateLabelRelative(r.date_value)]
      .filter(Boolean).join(" · ") || "확인 전",
    // 담당자가 못 받은 긴급은 반드시 눈에 띄어야 한다. 어르신은 안내를 듣고
    // 끊었는데 아무도 다시 걸지 않는 상태가 제일 위험하다.
    alert: r.transfer_status && r.transfer_status !== "연결됨"
      ? `담당자 연결 ${r.transfer_status} — 어르신께 다시 연락이 필요해요` : null,
    meta: `${r.channel || "전화"} · #${r.id}`,
    selected: state.selectedIntake === r.id,
    onClick: () => openIntake(r.id),
  });
}

// ── 오른쪽: 접수카드 ────────────────────────────────────────
function detailPane() {
  if (state.error) return el("div", "wide-pane", [errorBox(state.error)]);
  if (!state.selectedIntake) {
    return el("div", "detail-pane", [empty("왼쪽에서 요청을 고르면 접수카드가 열려요.")]);
  }
  const d = state.intakeDetail;
  if (!d) return el("div", "detail-pane", [empty("불러오는 중…")]);

  // **긴급은 접수카드가 없다.** AI 가 응급 여부를 판정하지 않기 때문에
  // 카드를 만들다 말고 사람에게 넘긴다(pipeline). 그래서 card 가 null 인데,
  // 카드 화면을 그대로 그리면 항목이 전부 '—' 인 빈 표가 뜬다 — 시연 장면 3 이
  // 이 경로다. 무엇을 해야 하는지를 보여주는 다른 화면이어야 한다.
  if (d.status === "긴급" || d.status === "긴급 처리됨" || !d.card) return urgentPane(d);

  const card = d.card || {};
  const g = d.gate || {};
  const f = card.fields || {};

  // target 은 "박순자 (보호자 대리 요청 — 확인 필요)" 처럼 설명이 괄호로
  // 붙어서 온다. 제목에 그대로 쓰면 두 줄로 접히고, 무엇이 이름이고 무엇이
  // 단서인지 구분이 안 된다. 이름만 제목으로 두고 나머지는 배지로 뗀다.
  const [name, note] = splitTarget(card.target);

  return el("div", "detail-pane", [
    el("div", "detail-head", [
      el("h1", null, name),
      note ? badge(note, "need") : null,
      el("span", "sub", card.phone_masked || ""),
      el("div", "right",
        `${d.channel || "전화"} 접수 · ${d.created_at || ""}`),
    ]),
    el("div", "cols", [
      el("div", "col", [heardSection(card), gateSection(d, g)]),
      el("div", "col", [visitSection(card, f), supportSection(card), guardianSection(card)]),
    ]),
    footbar(d, g),
  ]);
}

// ── 긴급 ────────────────────────────────────────────────────
//
// 여기서 보여줄 것은 접수 내용이 아니라 **지금 사람이 무엇을 해야 하는가**다.
// 병원·방문일 같은 칸을 비워서 늘어놓으면 "아직 안 채워진 접수" 처럼 보이는데,
// 긴급은 채우다 만 것이 아니라 **일부러 만들지 않은** 것이다.
function urgentPane(d) {
  const done = d.status === "긴급 처리됨";
  const connected = d.transfer_status === "연결됨";

  return el("div", "detail-pane", [
    el("div", "detail-head", [
      el("h1", null, splitTarget(d.target)[0]),
      badge(done ? "긴급 처리됨" : "긴급", done ? "ok" : "urgent"),
      el("div", "right", `${d.channel || "전화"} 접수 · ${d.created_at || ""}`),
    ]),
    el("div", "cols", [
      el("div", "col", [
        sectionTitle("요청 내용", "원문 그대로"),
        el("div", "quote", [
          el("div", "who", "어르신"),
          el("div", "txt", `“${d.raw_utterance || "—"}”`),
        ]),
        sectionTitle("접수카드를 만들지 않았습니다"),
        el("div", "ask",
          "동행고리 AI 는 응급 여부를 판단하지 않습니다. 긴급 신호가 잡히면 "
          + "카드 생성을 중단하고 담당자·사람 상담으로 넘깁니다."),
      ]),
      el("div", "col", [
        sectionTitle("담당자 연결"),
        // 담당자가 못 받은 건은 반드시 눈에 띄어야 한다. 어르신은 안내를 듣고
        // 끊었는데 아무도 다시 걸지 않는 상태가 제일 위험하다.
        frow("전환 결과", d.transfer_status || "기록 없음", {
          right: d.transfer_status
            ? badge(d.transfer_status, connected ? "ok" : "urgent") : null,
          evidence: connected ? [] : ["어르신께 다시 연락이 필요해요."],
        }),
        frow("연락처", d.phone),
        done ? null : resolveBox(d),
      ]),
    ]),
  ]);
}

function resolveBox(d) {
  if (!can("intake.confirm")) {
    return el("div", "ask", "처리 완료 표시는 사회복지사가 합니다.");
  }
  const note = el("input");
  note.placeholder = "어떻게 처리했는지 (감사 로그에 남아요)";
  const go = button("처리 완료로 표시", "btn primary", async () => {
    go.disabled = true;
    try {
      await api.resolveUrgent(d.id, note.value.trim());
      await openIntake(d.id);
      await reload();
    } catch (e) {
      update({ error: e });
    }
  });
  note.onkeydown = (e) => { if (e.key === "Enter") go.click(); };
  return el("div", "frow needbox", [
    el("div", "vl", [
      el("div", null, "연락을 마쳤으면 표시해 주세요"),
      el("div", "ask", "확정과는 다릅니다 — 긴급은 확정할 카드가 없습니다."),
      el("div", "verify", [note, go]),
    ]),
  ]);
}

// 왼쪽 단 — 어르신이 무엇을 말했나
function heardSection(card) {
  const kids = [
    sectionTitle("요청 내용", card.raw_utterance ? "원문 그대로" : null),
    el("div", "quote", [
      el("div", "who", "어르신"),
      el("div", "txt", `“${card.raw_utterance || "—"}”`),
    ]),
  ];

  // 전화 2턴에서 받은 본인 확인 답변. AI 가 확정한 것이 아니라 들은 말이다.
  if (card.identity_answer) {
    kids.push(el("div", null, [
      sectionTitle("확인 과정"),
      el("div", "ask", "자동 안내 — “○○○ 님 맞으실까요?”"),
      el("div", null, [
        document.createTextNode(`어르신 답변: “${card.identity_answer}” `),
        badge(card.identity_status || "확인 필요"),
      ]),
    ]));
  }
  if (card.spoken_name || card.spoken_region) {
    kids.push(el("div", "ask",
      `말한 성함 ${card.spoken_name || "—"} · 말한 주소 ${card.spoken_region || "—"}`));
  }
  return el("div", null, kids);
}

// 막힌 항목 — 되물을 질문과 답 적는 칸
function gateSection(d, g) {
  const blockers = g.blockers || [];
  if (!blockers.length) return null;
  return el("div", null, [
    sectionTitle(`남은 확인 ${blockers.length}건`,
                 "확정하려면 먼저 확인해야 해요"),
    ...blockers.map((b) => blockerBox(d, b)),
  ]);
}

function blockerBox(d, b) {
  const input = el("input");
  input.placeholder = "통화로 확인한 값";
  const save = button("확인 완료로 저장", "btn sm", async () => {
    const v = input.value.trim();
    if (!v) return;
    save.disabled = true;
    try {
      await api.verifyField(d.id, b.field, v);
      await openIntake(d.id);           // 게이트가 풀렸는지 다시 받아 그린다
    } catch (e) {
      alertInline(input, e.message);
      save.disabled = false;
    }
  });
  input.onkeydown = (e) => { if (e.key === "Enter") save.click(); };

  return el("div", "frow needbox", [
    el("div", "vl", [
      el("div", null, [document.createTextNode(b.label + " "), badge("확인 필요")]),
      // 질문을 함께 띄우는 게 핵심이다. 이 화면을 띄운 채 어르신께 전화를 걸어
      // 그대로 물어보고 답을 바로 적는 자리다.
      b.question ? el("div", "ask", `추천 질문 — “${b.question}”`) : null,
      // 통화에서 받아 적은 성함·읍면동. 있으면 "성함이 어떻게 되세요"가 아니라
      // "김말자 님 맞으실까요"로 물을 수 있다. {label, value} 객체로 온다.
      ...(b.heard || []).map((h) => el("div", "ask", `들은 ${h.label} — ${h.value}`)),
      el("div", "verify", [input, save]),
    ]),
  ]);
}

function alertInline(input, msg) {
  const box = input.closest(".vl");
  if (box) box.append(el("div", "err", msg));
}

// 오른쪽 단 — 그래서 무엇을 하기로 했나
function visitSection(card, f) {
  return el("div", null, [
    sectionTitle("방문 정보"),
    frow("요청 유형", card.intent),
    fieldRow("방문일", f.date, dateLabelRelative(f.date?.value)),
    fieldRow("병원", f.hospital),
    fieldRow("진료과", f.dept),
    fieldRow("예약 시각", f.time),
  ]);
}

// 상태·근거를 함께 보여주는 항목 행.
//
// **확률(%)은 쓰지 않는다.** 상태 3단계와 근거 문장으로만 말한다 — 86% 라는
// 숫자는 복지사가 판단에 쓸 수 없지만 "최근 6개월 내 2회 방문" 은 쓸 수 있다.
function fieldRow(label, field, displayValue) {
  if (!field) return frow(label, null);
  const shown = displayValue || field.value || field.spoken;
  return frow(label, shown, {
    evidence: field.evidence || [],
    right: field.status ? badge(field.status) : null,
  });
}

function supportSection(card) {
  if (!card.need_level) return null;
  return el("div", null, [
    sectionTitle("이동 지원"),
    frow("동행 수준", card.need_level, {
      evidence: card.need_reasons || [],
      // 공식 근거(장기요양등급)인지 관찰 특성인지를 구분해서 보여준다.
      // 둘을 같은 무게로 적으면 근거의 강도가 사라진다.
      right: card.need_official === false ? badge("관찰 기준", "guess") : null,
    }),
  ]);
}

function guardianSection(card) {
  const g = card.guardian_contact;
  if (!g) return null;
  return el("div", null, [
    sectionTitle("보호자"),
    frow(g.relation ? `${g.name} (${g.relation})` : (g.name || "보호자"),
         g.phone, { evidence: g.available ? [`${g.available} 통화 가능`] : [] }),
  ]);
}

// ── 하단 고정 바 — 확정 ─────────────────────────────────────
function footbar(d, g) {
  if (d.status === "확정") {
    return el("div", "footbar", [el("div", "msg", "확정된 접수예요."), el("div", "grow")]);
  }
  if (!can("intake.confirm")) {
    return el("div", "footbar", [
      el("div", "msg", "확정 권한이 없어요 — 사회복지사가 확정합니다."),
      el("div", "grow"),
    ]);
  }
  const blockers = g.blockers || [];
  const kids = [];

  if (blockers.length) {
    kids.push(el("div", "msg",
      `확인할 내용 ${blockers.length}건 — ${blockers.map((b) => b.label).join(", ")}`));
  } else {
    kids.push(el("div", "msg", ""));
  }
  kids.push(el("div", "grow"));

  if (!blockers.length) {
    kids.push(button("접수카드 확정", "btn primary", () => confirmIntake(d, false, null)));
  } else if (!g.hard_block) {
    // 확인 없이 넘어갈 때는 이유를 함께 받는다. 사고가 났을 때 "연락이 닿지
    // 않았다"와 "물어볼 필요 없다고 봤다"는 책임이 전혀 다른데, 감사 로그에
    // '미확인 확정'만 남으면 그 둘을 구분할 수 없다.
    const why = el("select");
    for (const [v, t] of [["", "넘어가는 이유를 고르세요"],
                          ["연락이 닿지 않음", "연락이 닿지 않음"],
                          ["이미 알고 있음", "이미 알고 있음"],
                          ["물어볼 필요 없음", "물어볼 필요 없음"],
                          ["기타", "기타"]]) {
      const o = el("option", null, t); o.value = v; why.append(o);
    }
    const go = button("이대로 확정", "btn primary",
                      () => confirmIntake(d, true, why.value));
    go.disabled = true;
    why.onchange = () => { go.disabled = !why.value; };
    kids.push(why, go);
  } else {
    kids.push(el("div", "msg", "기관 규칙상 확인 없이는 확정할 수 없어요."));
  }
  return el("div", "footbar", kids);
}

async function confirmIntake(d, acknowledge, reason) {
  const card = d.card || {};
  const f = card.fields || {};
  try {
    await api.confirmIntake(d.id, {
      hospital: f.hospital?.value || card.hospital || "",
      date: f.date?.value || card.date_value || "",
      level: card.need_level || "",
      acknowledge,
      acknowledgeReason: reason || null,
    });
    update({ selectedIntake: null, intakeDetail: null });
    await reload();
  } catch (e) {
    // 409 는 요청이 틀린 게 아니라 지금 상태에서 확정할 수 없다는 뜻이다.
    // 화면을 열 때 게이트를 확인했으므로, 여기 걸리는 건 그 사이 상태가
    // 바뀐 경우다 — 최신 상태로 다시 그린다.
    if (e.status === 409) await openIntake(d.id);
    else update({ error: e });
  }
}
