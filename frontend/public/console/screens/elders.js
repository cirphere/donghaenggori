// 어르신 — 대상자 목록과 케어 프로필.
//
// 목록과 상세를 따로 받는다. 상세에 담긴 것이 건강 상태·보호자 연락처·독거
// 여부라, 목록 한 번에 스무 명 분이 통째로 나오면 **화면을 여는 것 자체가
// 개인정보 열람**이 된다. 서버도 그래서 두 엔드포인트로 나눠 두었다.

import { badge, dateLabel, el, empty, errorBox, frow, listRow, sectionTitle } from "../ui.js";
import { openProfile, reload, state, update } from "../app.js";

export function renderElders() {
  return el("div", "main", [listPane(), detailPane()]);
}

function listPane() {
  const q = el("input");
  q.placeholder = "이름 또는 전화번호로 검색";
  q.value = state.profileQuery;
  // 글자마다 서버를 찌르지 않는다. 엔터를 쳐야 찾는다 — 스무 명 규모에서
  // 실시간 검색은 서버만 바쁘고 사람에게는 차이가 없다.
  q.onkeydown = (e) => {
    if (e.key === "Enter") { state.profileQuery = q.value.trim(); reload(); }
  };

  return el("div", "list-pane", [
    el("div", "pane-head", [
      el("h2", null, [document.createTextNode("어르신"),
                      el("span", "sub", `${state.profiles.length}명`)]),
      el("div", null, [q]),
    ]),
    el("div", "pane-scroll",
      state.profiles.length
        ? state.profiles.map(rowOf)
        : [empty(state.profileQuery ? "찾는 분이 없어요." : "등록된 어르신이 없어요.")]),
  ]);
}

function rowOf(p) {
  return listRow({
    name: `${p.name} · ${p.age}세`,
    // 처음 오시는 분은 눈에 띄게 — 이력이 없으면 병원 후보를 낼 근거도 없다
    right: p.visits ? null : badge("신규", "guess"),
    sub: p.region,
    meta: p.last_visit ? `최근 동행 ${dateLabel(p.last_visit)} · ${p.visits}회`
                       : "동행 이력 없음",
    selected: state.selectedProfile === p.phone,
    onClick: () => openProfile(p.phone),
  });
}

function detailPane() {
  if (state.error) return el("div", "wide-pane", [errorBox(state.error)]);
  if (!state.selectedProfile) {
    return el("div", "detail-pane", [empty("왼쪽에서 어르신을 고르면 프로필이 열려요.")]);
  }
  const p = state.profileDetail;
  if (!p) return el("div", "detail-pane", [empty("불러오는 중…")]);

  const hist = p.history || [];
  return el("div", "detail-pane", [
    el("div", "detail-head", [
      el("h1", null, `${p.name} · ${p.age}세`),
      el("span", "sub", p.region || ""),
    ]),
    el("div", "cols", [
      el("div", "col", [
        sectionTitle("기본 정보"),
        frow("전화", p.phone),
        frow("보호자", guardianText(p.guardian)),
        sectionTitle("이동 지원"),
        frow("거동", p.mobility, {
          evidence: [
            p.fall_risk ? "낙상 위험" : null,
            p.lives_alone ? "독거" : null,
            p.ltci_grade ? `장기요양 ${p.ltci_grade}` : null,
            p.care_program ? `노인맞춤돌봄 ${p.care_program}` : null,
          ].filter(Boolean),
        }),
        frow("선호 시간", p.preferred_time),
        p.notes ? el("div", null, [sectionTitle("참고할 정보", "동행에 도움이 되는 내용"),
                                   el("div", "ask", p.notes)]) : null,
      ]),
      el("div", "col", [
        sectionTitle("자주 이용하는 병원"),
        ...(topHospitals(hist).length
          ? topHospitals(hist).map((h) => frow(h.hospital, `${h.dept} · ${h.count}회`,
              { evidence: [`최근 ${dateLabel(h.last)}`] }))
          : [empty("동행 이력이 없어요.")]),
        sectionTitle("최근 동행"),
        ...(hist.length
          ? hist.slice(-6).reverse().map((h) => frow(dateLabel(h.date),
              [h.hospital, h.dept].filter(Boolean).join(" · "),
              { evidence: [h.symptom, h.pharmacy ? "약국 들름" : null].filter(Boolean) }))
          : [empty("아직 동행한 기록이 없어요.")]),
      ]),
    ]),
  ]);
}

function guardianText(g) {
  if (!g) return null;
  const who = g.relation ? `${g.name} (${g.relation})` : g.name;
  return [who, g.phone, g.available].filter(Boolean).join(" · ");
}

/** 이력을 병원별로 묶어 자주 간 순서로 */
function topHospitals(history) {
  const agg = {};
  for (const h of history) {
    if (!h.hospital) continue;
    const a = (agg[h.hospital] ||= { hospital: h.hospital, dept: h.dept, count: 0, last: "" });
    a.count += 1;
    if ((h.date || "") > a.last) a.last = h.date;
  }
  return Object.values(agg).sort((a, b) => b.count - a.count || b.last.localeCompare(a.last));
}
