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
function hero() {
  return el("section", "hero", [
    el("div", "tag", "전남 어르신 병원동행 서비스"),
    el("h1", null, [
      document.createTextNode("병원 가는 길,"), el("br"),
      document.createTextNode("혼자 준비하지 마세요."),
    ]),
    el("p", null, "어르신의 병원 일정과 필요한 도움을 알려주세요. "
      + "동행고리AI가 신청 내용을 정리하고, 담당 사회복지사가 확인합니다."),
    el("div", "cta", [
      button("병원동행 신청하기", "btn primary", () => { step = 1; go("step"); }),
      button("신청 내용 확인", "btn line", () => go("lookup")),
    ]),
    el("div", "phone", [
      document.createTextNode("전화로도 신청하실 수 있어요 · "),
      el("b", null, "070-5275-3856"),
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
