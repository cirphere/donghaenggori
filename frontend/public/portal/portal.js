// 보호자 포털 — 어르신 가족이 병원동행을 신청하고 진행 상황을 본다.
//
// 사회복지사 콘솔과 쓰는 사람이 다르다. 자녀가 휴대폰으로 열고, 한 번에 하나만
// 묻고, 우리 용어를 쓰지 않는다. 화면에서 '접수카드'·'확인 필요'·'동행 수준'
// 같은 말이 보이면 안 된다.
//
// **여기서 하는 일은 신청과 조회 둘뿐이다.** 목록도 없고 남의 신청을 볼 방법도
// 없다. 조회는 신청번호와 연락처가 **둘 다** 맞아야 열린다.

import { api } from "../api.js";

const $ = (id) => document.getElementById(id);

function el(tag, cls, kids) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (kids == null) return n;
  if (Array.isArray(kids)) { for (const k of kids) if (k) n.append(k); }
  else n.textContent = String(kids);
  return n;
}

function button(label, cls, onClick) {
  const b = el("button", cls || "btn", label);
  if (onClick) b.onclick = onClick;
  return b;
}

// ── 상태 ────────────────────────────────────────────────────
const HELP = ["이동 도움", "휠체어 이동", "접수·수납 도움",
              "진료실 동행", "약국 동행", "보호자 연락"];

const form = {
  name: "", age: "", region: "", relation: "", guardianPhone: "",
  date: "", dateUnknown: false, time: "", timeUnknown: false,
  hospital: "", hospitalUnknown: false, dept: "",
  help: [], helpUnknown: false,
  note: "",
};

let view = "hero";     // hero | step | done | lookup | status
let step = 1;
let result = null;     // 접수 결과 {access_code, urgent…}
let status = null;     // 조회 결과
let error = "";

const STEPS = 6;

// ── 렌더 ────────────────────────────────────────────────────
function render() {
  const main = $("main");
  main.replaceChildren();
  try {
    main.append(({ hero, step: stepView, done, lookup, status: statusView })[view]());
  } catch (e) {
    console.error("[portal]", e);
    main.append(el("div", "wrap", [el("div", "err", "화면을 그리지 못했습니다 — " + e.message)]));
  }
  window.scrollTo(0, 0);
}

function go(v) { view = v; error = ""; render(); }

// ── 첫 화면 ─────────────────────────────────────────────────
//
// 히어로만 두면 "신청하기 버튼 하나 있는 페이지"가 된다. 아래 섹션들이
// 하는 일이 따로 있다 — **전화로도 된다는 것을 설명하는 것**이다.
//
// 이 서비스의 주된 입구는 웹이 아니라 전화다. 어르신은 앱을 깔지 않고,
// 이 화면을 여는 사람은 대개 자녀다. 자녀에게 "부모님은 전화만 하시면
// 된다"를 납득시키지 못하면 서비스가 시작되지 않는다.
function hero() {
  const startBtn = () => button("병원동행 신청하기", "btn primary",
                                () => { step = 1; go("step"); });
  return el("div", null, [
    el("section", "hero", [
      el("div", "tag", "전남 어르신 병원동행 서비스"),
      el("h1", null, [
        document.createTextNode("병원 가는 길,"), el("br"),
        document.createTextNode("혼자 준비하지 마세요."),
      ]),
      el("p", null, "어르신의 병원 일정과 필요한 도움을 알려주세요. "
        + "동행고리AI가 신청 내용을 정리하고, 담당 사회복지사가 확인합니다."),
      el("div", "cta", [
        startBtn(),
        button("신청 내용 확인", "btn line", () => go("lookup")),
      ]),
      el("div", "phone", [
        document.createTextNode("전화로도 신청하실 수 있어요 · "),
        el("b", null, "070-5275-3856"),
      ]),
    ]),
    howSection(),
    phoneSection(),
    promiseSection(),
    closingSection(startBtn),
    footer(),
  ]);
}

/** 신청은 이렇게 진행돼요 — 3단계 */
function howSection() {
  const steps = [
    ["01", "필요한 내용을 알려주세요", "어르신과 병원 방문 정보를 간단하게 입력합니다."],
    ["02", "담당자가 확인해요",
     "신청 내용을 담당 사회복지사가 확인하고 필요한 내용을 다시 확인합니다."],
    ["03", "확정된 일정에 맞춰 함께해요", "확정된 일정에 맞춰 병원동행이 진행됩니다."],
  ];
  return el("section", "sec sec-cream", [
    el("div", "sec-in", [
      el("h2", "sec-h", "신청은 이렇게 진행돼요"),
      el("p", "sec-p", "복잡한 절차 없이, 휴대폰으로 몇 분이면 충분해요."),
      el("div", "how", steps.map(([n, t, s]) => el("div", "how-i", [
        el("div", "how-n", n),
        el("div", "how-t", t),
        el("div", "how-s", s),
      ]))),
    ]),
  ]);
}

/** AI 전화 신청 — 전화가 어떻게 접수가 되는지 4단계로 보여준다.
 *
 *  **여기가 이 페이지에서 제일 중요한 자리다.** 시연에서 말하는 것과 같은
 *  내용을 화면으로 보여준다: 평소처럼 말하면 → 맥락을 이해하고 → 모르는 것만
 *  되묻고 → 접수 정보로 정리된다. 마지막 확인은 사람이 한다. */
function phoneSection() {
  return el("section", "sec", [
    el("div", "sec-in", [
      el("div", "tag", "AI 전화 신청"),
      el("h2", "sec-h left", [
        document.createTextNode("전화 한 통이면,"), el("br"),
        document.createTextNode("동행 신청이 시작돼요."),
      ]),
      el("p", "sec-p left", "익숙한 말 그대로 이야기하세요. "
        + "동행고리AI가 필요한 내용을 듣고 정리해드려요."),

      el("div", "steps4", [
        stepCard("STEP 01 · 평소처럼 말해요", [
          el("div", "call", [
            el("div", "call-t", "동행고리AI"),
            el("div", "call-s", "통화 중 · 00:12"),
            el("div", "bubble", "“나 모레 저번에 무릎 봐준 데 가야겄어.”"),
          ]),
        ], "익숙한 전화로, 평소 말하듯 이야기하면 돼요. 앱 설치나 복잡한 입력은 필요하지 않아요."),

        stepCard("STEP 02 · 말의 맥락을 이해해요", [
          mapRow("“모레”", "8월 19일"),
          mapRow("“저번에 무릎 봐준 데”", "지난 동행 기록 · 정형외과"),
          mapRow("“가야겄어”", "병원동행 요청"),
        ], "단어만 받아 적지 않아요. 지난 동행 정보와 대화의 맥락을 함께 살펴봐요."),

        stepCard("STEP 03 · 모르는 내용만 다시 확인해요", [
          el("div", "chat", [
            el("div", "said ai", "지난번에 방문하셨던 정형외과 말씀하시는 게 맞을까요?"),
            el("div", "said me", "응, 거기."),
            el("div", "said ai", "알겠습니다. 몇 시까지 병원에 가셔야 하나요?"),
          ]),
        ], "확실하지 않은 정보는 임의로 결정하지 않고, 필요한 내용만 다시 확인해요."),

        stepCard("STEP 04 · 접수 정보로 정리돼요", [
          el("div", "mini", [
            el("div", "mini-h", [
              el("b", null, "병원동행 요청"),
              el("span", "mini-tag", "새 요청"),
            ]),
            miniRow("어르신", "김영자"),
            miniRow("방문일", "8월 19일"),
            miniRow("병원", "성가롤로병원"),
            miniRow("진료과", "정형외과"),
            miniRow("예약시간", "확인 필요", true),
          ]),
        ], "통화가 끝나면 필요한 정보가 자동으로 정리됩니다. 마지막 확인은 담당자가 해요."),
      ]),

      el("div", "band", [
        el("div", "band-t", "마지막 확인은 담당자가 해요."),
        el("div", "band-s",
           "AI가 정리한 내용을 담당 사회복지사가 확인한 뒤 병원동행 일정이 확정됩니다."),
      ]),

      el("div", "roles", [
        roleCol("어르신", ["익숙한 전화로 이야기해요"], "070-5275-3856"),
        roleCol("보호자", ["웹에서 신청하고", "가족 정보를 관리해요"]),
        roleCol("담당자", ["내용을 확인하고", "일정을 확정해요"]),
      ]),

      el("div", "webline", [
        el("span", null, "직접 입력이 편하신가요?"),
        button("웹으로 신청하기", "btn line sm", () => { step = 1; go("step"); }),
      ]),
    ]),
  ]);
}

function stepCard(title, body, note) {
  return el("div", "s4", [
    el("div", "s4-t", title),
    el("div", "s4-b", body),
    el("div", "s4-n", note),
  ]);
}

function mapRow(from, to) {
  return el("div", "maprow", [
    el("span", "from", from),
    el("span", "arrow", "→"),
    el("span", "to", to),
  ]);
}

function miniRow(k, v, warn) {
  return el("div", "mini-r", [
    el("span", "k", k),
    el("span", warn ? "v warn" : "v", v),
  ]);
}

function roleCol(title, lines, phone) {
  return el("div", "role", [
    el("div", "role-t", title),
    ...lines.map((l) => el("div", "role-s", l)),
    phone ? el("div", "role-p", phone) : null,
  ]);
}

/** 신청해도 바로 확정되지 않는다는 것 — 기대를 미리 맞춘다.
 *
 *  이 문단이 없으면 보호자가 신청 직후 병원에 갈 준비를 한다. 확정은
 *  담당자가 하고, 그 사이에 확인 전화가 갈 수 있다는 것을 먼저 말해 둔다. */
function promiseSection() {
  return el("section", "sec sec-white", [
    el("div", "sec-in narrow", [
      // 이모지(🛡)를 쓰지 않는다. 기계마다 다른 그림이 나오고, 안드로이드에서는
      // 파란 방패가 뜬다. 인라인 SVG 로 두면 어디서나 같다.
      svg(44, 44, "#F94704", 1.8,
          "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z", "M9 12l2 2 4-4"),
      el("h2", "sec-h", "신청했다고 바로 확정되는 것은 아니에요."),
      el("p", "sec-p tight", [
        document.createTextNode("입력한 내용은 담당 사회복지사가 확인한 뒤 최종 일정과 지원 내용을 확정합니다."),
        el("br"),
        document.createTextNode("확인이 필요한 부분이 있으면 보호자님께 먼저 연락드려요."),
      ]),
      el("p", "sec-note", "AI는 신청 내용을 정리하는 역할을 하며, 의료적 판단이나 진단을 하지 않습니다."),
    ]),
  ]);
}

/** 선으로 그린 아이콘. 목업이 stroke 방식이라 그대로 맞춘다. */
function svg(w, h, stroke, width, ...paths) {
  const ns = "http://www.w3.org/2000/svg";
  const n = document.createElementNS(ns, "svg");
  n.setAttribute("width", w); n.setAttribute("height", h);
  n.setAttribute("viewBox", "0 0 24 24");
  n.setAttribute("fill", "none");
  n.setAttribute("stroke", stroke);
  n.setAttribute("stroke-width", width);
  n.setAttribute("stroke-linecap", "round");
  n.setAttribute("stroke-linejoin", "round");
  n.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    n.append(path);
  }
  return n;
}

function closingSection(startBtn) {
  // 언덕은 CSS 그라데이션으로는 안 된다 — 목업은 곡선 두 겹이 겹쳐 깊이를
  // 만든다. 그대로 SVG path 로 둔다(목업 좌표 그대로).
  const ns = "http://www.w3.org/2000/svg";
  const hills = document.createElementNS(ns, "svg");
  hills.setAttribute("viewBox", "0 0 1440 170");
  hills.setAttribute("preserveAspectRatio", "xMidYMax slice");
  hills.setAttribute("aria-hidden", "true");
  hills.classList.add("hills");
  for (const [d, fill] of [
    ["M0,90 Q360,20 720,70 T1440,60 L1440,170 0,170 Z", "#9BD07C"],
    ["M0,130 Q420,70 900,120 T1440,110 L1440,170 0,170 Z", "#7CBF5E"],
  ]) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", fill);
    hills.append(path);
  }
  // 나무 두 그루 — 목업 좌표
  for (const [tx, ty, rw, rh, cy, r, trunk, leaf] of [
    [200, 68, 6, 14, 16, 14, "#8A6238", "#5FA84D"],
    [1220, 90, 5, 12, 13, 11, "#8A6238", "#6CB558"],
  ]) {
    const g = document.createElementNS(ns, "g");
    g.setAttribute("transform", `translate(${tx},${ty})`);
    const rect = document.createElementNS(ns, "rect");
    rect.setAttribute("x", rw === 6 ? 9 : 8); rect.setAttribute("y", rw === 6 ? 26 : 20);
    rect.setAttribute("width", rw); rect.setAttribute("height", rh);
    rect.setAttribute("rx", rw / 2); rect.setAttribute("fill", trunk);
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", rw === 6 ? 12 : 10.5); c.setAttribute("cy", cy);
    c.setAttribute("r", r); c.setAttribute("fill", leaf);
    g.append(rect, c);
    hills.append(g);
  }

  return el("section", "closing", [
    hills,
    el("div", "closing-in", [
      el("h2", null, "지금 바로 신청해 보세요"),
      el("p", null, "3분이면 충분해요. 나머지는 담당자가 함께합니다."),
      el("div", "cta", [
        startBtn(),
        el("a", "callbtn", [
          svg(18, 18, "#F94704", 2,
              "M22 16.9v3a2 2 0 01-2.2 2 19.8 19.8 0 01-8.6-3.1 19.5 19.5 0 01-6-6A19.8 "
              + "19.8 0 012.1 4.2 2 2 0 014.1 2h3a2 2 0 012 1.7c.1 1 .4 2 .7 2.9a2 2 0 "
              + "01-.5 2.1L8 10a16 16 0 006 6l1.3-1.3a2 2 0 012.1-.5c.9.3 1.9.6 2.9.7a2 "
              + "2 0 011.7 2z"),
          el("span", null, "070-5275-3856"),
        ]),
      ]),
    ]),
  ]);
}

function footer() {
  return el("footer", "foot", [
    el("div", "sec-in", [
      el("div", "foot-b", "동행고리AI"),
      el("div", "foot-p", [
        document.createTextNode("전화 문의 "),
        el("b", null, "070-5275-3856"),
      ]),
      el("div", "foot-s", "동행고리AI는 의료적 진단이나 판단을 하지 않습니다. "
        + "최종 일정과 지원 내용은 담당자가 확인합니다."),
      el("div", "foot-s", "신청 확인과 연락을 위해 필요한 정보만 수집합니다."),
      el("div", "foot-c", "© 동행고리AI"),
    ]),
  ]);
}

// ── 단계 ────────────────────────────────────────────────────
function stepView() {
  const body = ({ 1: q1, 2: q2, 3: q3, 4: q4, 5: q5, 6: q6 })[step]();
  const bar = el("div", "bar", [el("i")]);
  bar.firstChild.style.width = `${(step / STEPS) * 100}%`;

  return el("div", null, [
    el("div", "wrap", [
      el("div", "step-top", [
        button("‹", "btn text", () => (step > 1 ? (step--, render()) : go("hero"))),
        el("div", "mid", "신청하기"),
        el("div", null, `${step} / ${STEPS}`),
      ]),
      bar,
      body,
    ]),
    el("div", "footfix", [el("div", "inner", [
      error ? el("div", "err", error) : null,
      step < STEPS
        ? button("다음", "btn primary", onNext)
        : button("신청하기", "btn primary", submit),
    ])]),
  ]);
}

function field(label, node, { required, hint } = {}) {
  return el("div", "field", [
    el("label", null, [
      document.createTextNode(label),
      required ? el("span", "req", " *") : null,
    ]),
    node,
    hint ? el("div", "hint", hint) : null,
  ]);
}

function input(key, placeholder, type = "text") {
  const n = el("input");
  n.type = type;
  n.placeholder = placeholder || "";
  n.value = form[key] || "";
  n.oninput = () => { form[key] = n.value; };
  return n;
}

/** 하나만 고르는 칩 묶음 */
function picks(options, key) {
  const box = el("div", "picks");
  for (const o of options) {
    const b = el("button", "pick" + (form[key] === o ? " on" : ""), o);
    b.onclick = () => { form[key] = form[key] === o ? "" : o; render(); };
    box.append(b);
  }
  return box;
}

/** 여러 개 고르는 칩 묶음 */
function multi(options, key) {
  const box = el("div", "picks");
  for (const o of options) {
    const b = el("button", "pick" + (form[key].includes(o) ? " on" : ""), o);
    b.onclick = () => {
      form[key] = form[key].includes(o) ? form[key].filter((x) => x !== o) : [...form[key], o];
      form.helpUnknown = false;
      render();
    };
    box.append(b);
  }
  return box;
}

/** "아직 몰라요" 토글 — 모르는 것을 모른다고 넣을 수 있어야 한다.
 *  억지로 채우게 하면 보호자가 아무 값이나 적고, 그게 확정으로 이어진다. */
function unknown(label, key, clear) {
  const b = el("button", "pick" + (form[key] ? " on" : ""), label);
  b.onclick = () => {
    form[key] = !form[key];
    if (form[key]) for (const k of clear || []) form[k] = Array.isArray(form[k]) ? [] : "";
    render();
  };
  return b;
}

function q1() {
  return el("div", null, [
    el("div", "q", "누구의 병원 방문인가요?"),
    field("어르신 성함", input("name", "예: 김순자"), { required: true }),
    field("생년 또는 나이", input("age", "예: 1943년생 또는 83세")),
    field("거주 지역", input("region", "예: 나주시 금천면")),
    field("보호자와의 관계", picks(["딸", "아들", "배우자", "기타"], "relation")),
    field("보호자 연락처", input("guardianPhone", "예: 010-1234-5678", "tel"),
          { required: true, hint: "신청 확인과 연락을 위해 필요한 정보만 받습니다." }),
  ]);
}

function q2() {
  return el("div", null, [
    el("div", "q", "언제 병원에 가시나요?"),
    el("div", "q-sub", "아직 정확하지 않으셔도 괜찮아요. 담당자가 함께 확인합니다."),
    field("날짜", el("div", null, [
      form.dateUnknown ? null : input("date", "", "date"),
      el("div", "picks", [unknown("아직 정해지지 않았어요", "dateUnknown", ["date"])]),
    ])),
    field("시간", el("div", null, [
      form.timeUnknown ? null : input("time", "", "time"),
      el("div", "picks", [unknown("시간을 아직 몰라요", "timeUnknown", ["time"])]),
    ]), { hint: "입력하신 일정은 담당자 확인 후에 확정됩니다." }),
  ]);
}

function q3() {
  return el("div", null, [
    el("div", "q", "어느 병원에 가시나요?"),
    field("병원명", el("div", null, [
      form.hospitalUnknown ? null : input("hospital", "예: 화순전남대학교병원"),
      el("div", "picks", [unknown("잘 모르겠어요", "hospitalUnknown", ["hospital"])]),
    ]), { required: !form.hospitalUnknown }),
    field("진료과", input("dept", "예: 정형외과")),
  ]);
}

function q4() {
  return el("div", null, [
    el("div", "q", "어떤 도움이 필요하신가요?"),
    el("div", "q-sub", "필요한 도움을 모두 골라 주세요."),
    multi(HELP, "help"),
    el("div", "picks", [
      (() => {
        const b = el("button", "pick" + (form.helpUnknown ? " on" : ""), "잘 모르겠어요");
        b.onclick = () => { form.helpUnknown = !form.helpUnknown; form.help = []; render(); };
        b.style.marginTop = "8px";
        return b;
      })(),
    ]),
  ]);
}

function q5() {
  const t = el("textarea");
  t.placeholder = "예: 귀가 어두우셔서 큰 소리로 안내해 주시면 좋겠어요.";
  t.value = form.note;
  t.oninput = () => { form.note = t.value; };
  return el("div", null, [
    el("div", "q", "추가로 알려주실 내용이 있나요?"),
    el("div", "q-sub", "적어주신 내용은 담당자에게 그대로 전달됩니다."),
    field("추가 내용", t, { hint: "건너뛰셔도 괜찮아요." }),
  ]);
}

function q6() {
  const row = (k, v, to) => el("div", "rrow", [
    el("div", "k", k),
    el("div", "v", v || "미입력"),
    button("수정", "edit", () => { step = to; render(); }),
  ]);
  return el("div", null, [
    el("div", "q", "신청 내용을 확인해 주세요."),
    el("div", "q-sub", "담당 사회복지사가 확인한 뒤 최종 일정을 확정합니다."),
    el("div", "review", [
      row("어르신", [form.name, form.age, form.region].filter(Boolean).join(" · "), 1),
      row("날짜", form.dateUnknown ? "아직 정해지지 않음" : form.date, 2),
      row("시간", form.timeUnknown ? "아직 모름" : form.time, 2),
      row("병원", form.hospitalUnknown ? "잘 모르겠음" : form.hospital, 3),
      row("진료과", form.dept, 3),
      row("필요한 도움", form.helpUnknown ? "잘 모르겠음" : form.help.join(", "), 4),
      row("추가 내용", form.note, 5),
      row("보호자 연락처", form.guardianPhone, 1),
    ]),
    el("div", "note", "동행고리AI는 의료적 진단이나 판단을 하지 않으며, "
      + "최종 일정과 지원 내용은 담당자가 확인합니다."),
  ]);
}

function onNext() {
  error = "";
  if (step === 1) {
    if (!form.name.trim()) return fail("어르신 성함을 적어 주세요.");
    if (!form.guardianPhone.trim()) return fail("보호자 연락처를 적어 주세요.");
  }
  if (step === 3 && !form.hospitalUnknown && !form.hospital.trim()) {
    return fail("병원명을 적어 주시거나 '잘 모르겠어요'를 골라 주세요.");
  }
  if (step === 4 && !form.helpUnknown && !form.help.length) {
    return fail("필요한 도움을 하나 이상 골라 주세요. 모르시면 '잘 모르겠어요'를 고르시면 돼요.");
  }
  step += 1;
  render();
}

function fail(msg) { error = msg; render(); }

// ── 신청 ────────────────────────────────────────────────────
//
// 백엔드는 발화 한 문장을 받는다(`{phone, utterance}`). 폼을 문장으로 합쳐서
// 보내면 전화로 들어온 접수와 **같은 파이프라인**을 탄다 — 날짜 해석, 병원
// 판정, 긴급 감지가 한 곳에서만 돌아 화면마다 결과가 갈리지 않는다.
//
// 모르는 항목은 문장에 넣지 않는다. "병원 잘 모르겠음" 같은 말을 넣으면
// 병원명 추출이 그 문장을 물어뜯는다 — 비워 두면 '확인 필요'로 남아
// 복지사가 되묻는다. 그게 원래 설계다.
export function toUtterance(f) {
  const parts = [];
  const who = f.name.trim();
  if (who) parts.push(`${who} 어르신`);
  if (!f.dateUnknown && f.date) parts.push(dateWords(f.date));
  if (!f.timeUnknown && f.time) parts.push(timeWords(f.time));
  if (!f.hospitalUnknown && f.hospital.trim()) parts.push(f.hospital.trim());
  if (f.dept.trim()) parts.push(f.dept.trim());
  parts.push("모시고 가야 합니다.");

  const extra = [];
  if (!f.helpUnknown && f.help.length) extra.push(f.help.join(", ") + "이 필요합니다.");
  if (f.note.trim()) extra.push(f.note.trim());
  return [parts.join(" "), ...extra].join(" ").trim();
}

function dateWords(iso) {
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return iso;
  return `${d.getMonth() + 1}월 ${d.getDate()}일`;
}

function timeWords(hm) {
  const [h, m] = hm.split(":").map(Number);
  if (isNaN(h)) return hm;
  const ampm = h < 12 ? "오전" : "오후";
  const hh = h % 12 === 0 ? 12 : h % 12;
  return m ? `${ampm} ${hh}시 ${m}분` : `${ampm} ${hh}시`;
}

async function submit() {
  error = "";
  const utterance = toUtterance(form);
  render();
  try {
    result = await api.guardianIntake(form.guardianPhone.trim(), utterance);
    go("done");
  } catch (e) {
    fail("신청을 보내지 못했습니다 — " + e.message);
  }
}

// ── 완료 ────────────────────────────────────────────────────
function done() {
  // 긴급으로 판정되면 접수카드를 만들지 않는다. 보호자에게는 "접수됐다"가
  // 아니라 "지금 연락하시라"를 말해야 한다.
  if (result?.urgent) {
    return el("div", "wrap", [el("div", "done", [
      el("div", "mark", "!"),
      el("h2", null, "지금 바로 연락해 주세요"),
      el("p", null, "적어주신 내용이 급해 보입니다. 이 신청은 접수하지 않았습니다. "
        + "아래 번호로 전화해 주시거나, 위급하면 119에 연락해 주세요."),
      el("div", "codebox", [el("div", "v", "070-5275-3856")]),
      el("div", null, [button("처음으로", "btn text", () => go("hero"))]),
    ])]);
  }

  return el("div", "wrap", [el("div", "done", [
    el("div", "mark", "✓"),
    el("h2", null, "신청이 접수되었습니다."),
    el("p", null, "담당 사회복지사가 내용을 확인한 뒤 필요한 경우 보호자님께 연락드립니다."),
    el("div", "codebox", [
      el("div", "k", "신청번호"),
      el("div", "v", result?.access_code || "—"),
    ]),
    // 이 번호는 다시 알려줄 방법이 없다. 조회의 열쇠라 서버가 두 번 내보내지
    // 않기 때문이다 — 그 사실을 여기서 분명히 말해 둔다.
    el("div", "keep", "이 번호는 진행 상황을 확인하실 때 필요합니다. "
      + "화면을 닫으면 다시 보여드릴 수 없으니 적어 두시거나 화면을 저장해 주세요."),
    el("div", null, [
      button("신청 내용 확인", "btn primary", () => {
        lookupForm.code = result?.access_code || "";
        lookupForm.phone = form.guardianPhone;
        go("lookup");
      }),
    ]),
    el("div", null, [button("처음으로", "btn text", () => go("hero"))]),
  ])]);
}

// ── 조회 ────────────────────────────────────────────────────
const lookupForm = { code: "", phone: "" };

function lookup() {
  const code = el("input");
  code.placeholder = "DH-260817-XXXXXXXX";
  code.value = lookupForm.code;
  code.oninput = () => { lookupForm.code = code.value; };

  const phone = el("input");
  phone.type = "tel";
  phone.placeholder = "예: 010-1234-5678";
  phone.value = lookupForm.phone;
  phone.oninput = () => { lookupForm.phone = phone.value; };

  const go2 = button("확인하기", "btn primary", async () => {
    error = "";
    if (!lookupForm.code.trim() || !lookupForm.phone.trim()) {
      return fail("신청번호와 연락처를 모두 적어 주세요.");
    }
    go2.disabled = true;
    try {
      status = await api.guardianLookup(lookupForm.code.trim(), lookupForm.phone.trim());
      go("status");
    } catch (e) {
      // 서버는 무엇이 틀렸는지 알려주지 않는다 — 번호를 대입해 찾는 길을
      // 열어 주지 않으려는 것이다. 화면도 같은 말만 한다.
      fail(e.status === 429
        ? "시도가 너무 많습니다. 잠시 뒤에 다시 해주세요."
        : "신청번호와 연락처를 다시 확인해 주세요.");
      go2.disabled = false;
    }
  });

  return el("div", null, [
    el("div", "wrap", [
      el("div", "step-top", [
        button("‹", "btn text", () => go("hero")),
        el("div", "mid", "신청 확인"),
        el("div", null, ""),
      ]),
      el("div", "q", "신청번호를 알려주세요."),
      el("div", "q-sub", "접수하실 때 받으신 번호와 연락처가 모두 맞아야 열립니다."),
      field("신청번호", code, { required: true }),
      field("보호자 연락처", phone, { required: true }),
    ]),
    el("div", "footfix", [el("div", "inner", [
      error ? el("div", "err", error) : null, go2,
    ])]),
  ]);
}

function statusView() {
  const s = status || {};
  const steps = s.steps || [];
  const at = steps.indexOf(s.step);

  const desc = {
    "접수됨": "신청이 들어왔습니다.",
    "확인 중": "담당자가 신청 내용을 확인하고 있습니다.",
    "일정 확정": "일정이 확정되었습니다.",
    "동행 완료": "동행이 끝났습니다.",
  };

  return el("div", "wrap", [
    el("div", "step-top", [
      button("‹", "btn text", () => go("hero")),
      el("div", "mid", "신청 확인"),
      el("div", null, ""),
    ]),
    el("div", "q", "병원동행 신청"),
    el("div", "q-sub", `신청번호 ${s.code || ""}`),

    el("div", "review", [
      el("div", "rrow", [el("div", "k", "신청 내용"), el("div", "v", s.requested || "—")]),
      el("div", "rrow", [el("div", "k", "병원"),
                         el("div", "v", s.hospital || "담당자 확인 중")]),
      el("div", "rrow", [el("div", "k", "일정"),
                         el("div", "v", s.date ? `${s.date} ${s.time || ""}`.trim() : "담당자 확인 중")]),
      el("div", "rrow", [el("div", "k", "지원 내용"),
                         el("div", "v", s.level || "담당자 확인 중")]),
    ]),

    el("div", "track", steps.map((t, i) => el("div",
      "tstep" + (i < at ? " done" : i === at ? " now" : ""), [
        el("div", "dot"),
        el("div", null, [
          el("div", "t", t),
          i === at ? el("div", "s", desc[t] || "") : null,
        ]),
      ]))),

    el("div", "note", [
      document.createTextNode("일정과 지원 내용은 담당자 확인 후 확정됩니다. 궁금하신 점은 "),
      el("b", null, "070-5275-3856"),
      document.createTextNode("으로 문의해 주세요."),
    ]),
  ]);
}

// ── 시작 ────────────────────────────────────────────────────
$("btnLookupTop").onclick = () => go("lookup");
render();
