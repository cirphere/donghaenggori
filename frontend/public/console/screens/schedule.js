// 일정 — 확정된 접수가 곧 일정이다.
//
// **새 테이블을 만들지 않았다.** 목업도 "접수카드를 확정하면 일정이 여기에
// 생겨요"라고 적어 뒀고, 실제로 일정에 필요한 것은 확정된 접수에 거의 다
// 들어 있다 — 누가(target) 언제(confirmed_date) 어디로(confirmed_hospital)
// 어떤 지원으로(confirmed_level).
//
// 없는 것은 **동행 담당자**와 **수행 상태**(배정 필요 → 예정 → 완료) 둘뿐이다.
// 그 둘 때문에 테이블을 새로 파면 같은 사실이 두 곳에 적히고, 확정을 취소했을 때
// 어느 쪽이 진짜인지 알 수 없게 된다. 지금은 백엔드에 그 두 칸이 없어서
// 담당자는 비어 있고 상태는 날짜로만 가른다.
//
// TODO(본선 후): intakes 에 manager · sched_status 두 칸을 더한다.

import { badge, dateLabel, el, empty, errorBox, listRow } from "../ui.js";
import { openIntake, state, update } from "../app.js";

const FILTERS = [["전체", "전체"], ["배정 필요", "배정 필요"],
                 ["예정", "예정"], ["완료", "완료"]];

/** 확정된 접수를 일정 항목으로 바꾼다 — 홈 화면도 이걸 쓴다 */
export function scheduleOf(intakes) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return (intakes || [])
    .filter((r) => r.status === "확정" && (r.confirmed_date || r.date_value))
    .map((r) => {
      const date = r.confirmed_date || r.date_value;
      const d = new Date(date + "T00:00:00");
      return {
        id: r.id,
        date,
        time: r.time_value || "",
        target: r.target_age ? `${r.target} · ${r.target_age}세`
                             : (r.target || "미확인 대상자"),
        hospital: r.confirmed_hospital || r.hospital || "",
        dept: r.dept || "",
        level: r.confirmed_level || r.need_level || "",
        region: r.region || "",
        // 담당자 배정은 아직 백엔드에 없다. 있는 척 채우지 않는다 —
        // 화면에 이름이 뜨면 배정된 줄 알고 아무도 안 나간다.
        manager: r.manager || null,
        status: !r.manager ? "배정 필요" : (d < today ? "완료" : "예정"),
      };
    })
    .sort((a, b) => (a.date + a.time).localeCompare(b.date + b.time));
}

export function renderSchedule() {
  if (state.error) return el("div", "wide-pane", [errorBox(state.error)]);
  const all = scheduleOf(state.intakes);
  const rows = state.scheduleFilter === "전체"
    ? all : all.filter((s) => s.status === state.scheduleFilter);

  const groups = {};
  for (const s of rows) (groups[s.date] ||= []).push(s);

  return el("div", "main", [
    el("div", "list-pane", [
      el("div", "pane-head", [
        el("h2", null, [document.createTextNode("일정"),
                        el("span", "sub", `${all.length}건`)]),
      ]),
      el("div", "chips", FILTERS.map(([k, label]) => {
        const n = k === "전체" ? all.length : all.filter((s) => s.status === k).length;
        const c = el("button", "chip" + (k === state.scheduleFilter ? " on" : ""),
                     n && k !== "전체" ? `${label} ${n}` : label);
        c.onclick = () => update({ scheduleFilter: k });
        return c;
      })),
      el("div", "pane-scroll",
        rows.length
          ? Object.entries(groups).map(([date, items]) => el("div", null, [
              el("div", "pane-head", [el("div", "row-meta", dateLabel(date))]),
              ...items.map(schedRow),
            ]))
          : [empty("이 조건의 일정이 없어요. 접수카드를 확정하면 일정이 생겨요.")]),
    ]),
    detail(rows),
  ]);
}

function schedRow(s) {
  return listRow({
    name: `${s.time || "시간 미정"}  ${s.target}`,
    right: badge(s.status, s.status === "완료" ? "ok"
                         : s.status === "배정 필요" ? "need" : "plain"),
    sub: [s.hospital, s.dept].filter(Boolean).join(" · "),
    meta: s.level,
    selected: state.selectedIntake === s.id,
    onClick: () => openIntake(s.id),
  });
}

function detail(rows) {
  const s = rows.find((x) => x.id === state.selectedIntake);
  if (!s) return el("div", "detail-pane", [empty("일정을 고르면 자세히 볼 수 있어요.")]);
  const open = el("button", "btn ghost", "요청 보기");
  open.onclick = () => { update({ screen: "requests" }); openIntake(s.id); };

  return el("div", "detail-pane", [
    el("div", "detail-head", [
      el("h1", null, s.target),
      badge(s.status, s.status === "완료" ? "ok" : "plain"),
      el("div", "right", [open]),
    ]),
    el("div", "cols", [el("div", "col", [
      row("병원", [s.hospital, s.dept].filter(Boolean).join(" · ")),
      row("예약 시각", `${dateLabel(s.date)} ${s.time || "시간 미정"}`),
      row("출발지", s.region || "자택"),
      row("이동 지원", s.level),
      row("동행 담당자", s.manager || "아직 배정하지 않았어요"),
    ])]),
  ]);
}

function row(label, value) {
  return el("div", "frow", [el("div", "lb", label), el("div", "vl", value || "—")]);
}
