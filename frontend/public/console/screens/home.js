// 홈 — 아침에 화면을 켜면 제일 먼저 보는 곳.
//
// 목업의 구성을 그대로 옮겼다. 왼쪽 "먼저 처리할 일", 오른쪽 "오늘 일정".
// **오늘 할 일이 없으면 빈 화면이 맞다** — 채우려고 지표를 늘어놓으면
// 매일 아침 읽어야 할 것이 늘어나서, 정작 급한 것이 묻힌다.

import { badge, dateLabel, el, empty, errorBox } from "../ui.js";
import { openIntake, state, update } from "../app.js";
import { nameWithAge, pendingIntakes } from "./requests.js";
import { scheduleOf } from "./schedule.js";

export function renderHome() {
  if (state.error) return el("div", "wide-pane", [errorBox(state.error)]);
  const d = state.dashboard;
  if (!d) return el("div", "wide-pane", [empty("불러오는 중…")]);

  const intakes = state.intakes;
  const todos = pendingIntakes(intakes);
  const urgent = todos.filter((r) => r.status === "긴급").length;
  const sched = scheduleOf(intakes);
  const today = sched.filter((s) => s.date === todayIso());
  const unassigned = sched.filter((s) => s.status === "배정 필요").length;

  return el("div", "wide-pane", [
    el("h1", "home-h1", "오늘의 동행"),
    // 긴급은 '확인 필요'에 이미 들어 있다. 따로 더하면 6건짜리를 8건으로
    // 부풀려 말하게 된다 — 아침에 처음 보는 숫자라 틀리면 안 된다.
    el("div", "home-sub",
       `${dateLabel(todayIso())} · 오늘 일정 ${today.length}건 · `
       + `확인 필요 ${todos.length}건 · 배정 필요 ${unassigned}건`
       + (urgent ? ` (긴급 ${urgent}건)` : "")),
    el("div", "home-cols", [
      el("div", "col", [
        el("h3", "sec-title", `먼저 처리할 일 · ${todos.length}건`),
        ...(todos.length
          ? todos.map(todoRow)
          : [empty("지금 확인할 요청이 없어요.")]),
      ]),
      el("div", "col", [
        el("h3", "sec-title", `오늘 일정 · ${today.length}건`),
        ...(today.length
          ? today.map(schedRow)
          : [empty("오늘 잡힌 동행이 없어요.")]),
      ]),
    ]),
  ]);
}

function todoRow(r) {
  const urgent = r.status === "긴급";
  const open = el("button", "btn ghost", "요청 열기");
  open.onclick = () => { update({ screen: "requests" }); openIntake(r.id); };
  return el("div", "todo", [
    badge(r.status, urgent ? "urgent" : "need"),
    el("div", "body", [
      el("div", "t", `${nameWithAge(r)}${r.hospital ? " — " + r.hospital : ""}`),
      el("div", "s", [r.raw_utterance && `“${r.raw_utterance}”`, r.channel]
        .filter(Boolean).join(" · ")),
    ]),
    open,
  ]);
}

function schedRow(s) {
  return el("div", "sched-item", [
    el("div", "hm", s.time || "시간 미정"),
    el("div", "body", [
      el("div", "t", s.target),
      el("div", "s", [s.hospital, s.manager].filter(Boolean).join(" · ")),
    ]),
    el("div", null, [badge(s.status, s.status === "완료" ? "ok" : "plain")]),
  ]);
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
       + `-${String(d.getDate()).padStart(2, "0")}`;
}
