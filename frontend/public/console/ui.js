// 화면 조각을 만드는 최소 도구.
//
// **DOM 을 직접 만지지 않고 state → 화면을 다시 그린다.** 기존 staff.js 는
// 명령형으로 노드를 붙였다 지웠다 해서, 어디서 무엇이 바뀌는지 따라가기가
// 어려웠고 실제로 `append()` 체이닝 버그가 났다(append 는 undefined 를 돌려준다).
// 여기서는 화면마다 render(state) 하나만 두고, 바뀌면 통째로 다시 그린다.
//
// 나중에 React 로 옮길 때 이 구조가 그대로 컴포넌트가 된다.

/** el("div", "cls", "텍스트") 또는 el("div", "cls", [자식…]) */
export function el(tag, cls, kids) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (kids == null) return n;
  if (Array.isArray(kids)) {
    // null 을 걸러 낸다 — 조건부 자식을 `cond && el(...)` 로 쓰기 위해서다
    for (const k of kids) if (k) n.append(k);
  } else {
    n.textContent = String(kids);
  }
  return n;
}

export function button(label, cls, onClick) {
  const b = el("button", cls || "btn", label);
  if (onClick) b.onclick = onClick;
  return b;
}

/** 상태 배지 — 확인됨 / 추정 / 확인 필요 */
const STATUS_CLASS = { "확인됨": "ok", "추정": "guess", "확인 필요": "need" };
export function badge(text, cls) {
  return el("span", "badge " + (cls || STATUS_CLASS[text] || "plain"), text);
}

/** 라벨 | 값 | (근거) | 우측 액션 한 줄 */
export function frow(label, value, opts = {}) {
  const vl = el("div", "vl", [
    el("div", null, value == null || value === "" ? "—" : String(value)),
    ...(opts.evidence || []).map((e) => el("div", "ev", e)),
  ]);
  return el("div", "frow", [
    el("div", "lb", label),
    vl,
    opts.right ? el("div", "act", [opts.right]) : null,
  ]);
}

export function sectionTitle(text, note) {
  return el("h3", "sec-title", [
    document.createTextNode(text),
    note ? el("span", "note", note) : null,
  ]);
}

export function empty(text) {
  return el("div", "empty", text);
}

export function errorBox(e) {
  return el("div", "err", "불러오지 못했습니다 — " + (e && e.message ? e.message : e));
}

/** 필터 칩 묶음. items = [[key, label], …] */
export function chips(items, current, onPick) {
  return el("div", "chips", items.map(([key, label]) => {
    const c = el("button", "chip" + (key === current ? " on" : ""), label);
    c.onclick = () => onPick(key);
    return c;
  }));
}

/** 목록 한 행 */
export function listRow({ name, sub, meta, alert, right, selected, onClick }) {
  const r = el("button", "row" + (selected ? " on" : ""), [
    el("div", "row-top", [el("div", "row-name", name), right || null]),
    sub ? el("div", "row-sub", sub) : null,
    alert ? el("div", "row-alert", alert) : null,
    meta ? el("div", "row-meta", meta) : null,
  ]);
  if (onClick) r.onclick = onClick;
  return r;
}

// ── 표시용 형식 ─────────────────────────────────────────────
const WD = ["일", "월", "화", "수", "목", "금", "토"];

/** "2026-08-19" → "8월 19일 (화)" */
export function dateLabel(iso) {
  if (!iso) return null;
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return iso;
  return `${d.getMonth() + 1}월 ${d.getDate()}일 (${WD[d.getDay()]})`;
}

/** 오늘/내일이면 그렇게 부른다 — 날짜만 적으면 한 번 더 세어 봐야 한다 */
export function dateLabelRelative(iso) {
  if (!iso) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return iso;
  const diff = Math.round((d - today) / 86400000);
  const base = dateLabel(iso);
  if (diff === 0) return `오늘 (${base})`;
  if (diff === 1) return `내일 (${base})`;
  return base;
}

/** ISO 시각 문자열에서 HH:MM 만 */
export function hhmm(s) {
  if (!s) return "";
  const m = String(s).match(/(\d{1,2}):(\d{2})/);
  return m ? `${m[1].padStart(2, "0")}:${m[2]}` : "";
}
