// 감사 로그 — 누가 무엇을 확인하고 확정했는지.
//
// 목업에는 없다. 그런데 발표에서 기관 가치로 내세우는 것이 정확히 이것이고
// (`발표슬라이드.md` S10 "누가 확인하고 확정했는지 남는다"), 심사에서 물으면
// 화면으로 보여줄 수 있어야 한다. 말로만 하는 것과 열어서 보여주는 것은 다르다.
//
// 사회복지사·관리자만 본다(audit.view). 동행매니저에게는 네비에서 감춘다 —
// 자기가 담당하지 않은 어르신의 확정 이력까지 볼 이유가 없다.

import { api } from "../../api.js";
import { badge, el, empty, errorBox } from "../ui.js";
import { state, update } from "../app.js";

// 행동별 색. '미확인 확정'은 눈에 띄어야 한다 — 나중에 문제가 생겼을 때
// 제일 먼저 찾게 되는 줄이다.
const TONE = {
  "확정": "ok",
  "승인": "ok",
  "긴급처리": "plain",
  "거절": "guess",
  "확인": "guess",
  "미확인 확정": "urgent",
  "초기화": "urgent",
};

export function renderAudit() {
  if (state.error) return el("div", "wide-pane", [errorBox(state.error)]);
  const rows = state.audit || [];

  return el("div", "wide-pane", [
    el("h1", "home-h1", "감사 로그"),
    el("div", "home-sub",
       "확정·승인·수정이 전부 남습니다. 지울 수 없고, 화면에서 고칠 수도 없어요."),
    ...(rows.length ? rows.map(rowOf) : [empty("아직 기록이 없어요.")]),
  ]);
}

function rowOf(a) {
  return el("div", "frow", [
    el("div", "lb", (a.at || "").replace("T", " ")),
    el("div", "vl", [
      el("div", null, [
        badge(a.action, TONE[a.action] || "plain"),
        document.createTextNode(` ${a.actor || ""} (${a.role || ""})`),
      ]),
      a.detail ? el("div", "ev", a.detail) : null,
    ]),
    el("div", "act", el("span", "row-meta", `${a.target_type || ""} #${a.target_id || ""}`)),
  ]);
}

export async function loadAudit() {
  try {
    update({ audit: await api.audit(100), error: null });
  } catch (e) {
    update({ error: e });
  }
}
