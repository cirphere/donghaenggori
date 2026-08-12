// 보호자 웹 — 가족이 어르신 대신 병원동행을 신청한다.
//
// 사회복지사 화면과 **의도적으로 다르다.**
//   · 확정·승인 버튼이 없다. 보호자는 신청만 하고 확정은 담당자가 한다.
//   · 대시보드·감사 로그·다른 대상자 정보를 부르지 않는다.
//   · 접수 결과는 "이렇게 접수됐습니다" 수준으로만 보여준다 — 병원 후보의
//     내부 판정 근거까지 보호자에게 노출할 이유가 없다.
//
// 채널을 '앱·웹(보호자)'로 고정해 보낸다. 백엔드가 이 채널을 대리 접수로
// 처리해서, 발신번호를 대상자 번호로 착각하지 않고 후보로만 제시한다.

import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const CHANNEL = "앱·웹(보호자)";

function renderUrgent(d) {
  const box = el("div", "urgent " + (d.urgent_confident ? "hard" : "soft"));
  box.append(el("div", "urgent-title",
    d.urgent_confident ? "지금 바로 도움이 필요해 보입니다" : "내용을 확인하지 못했습니다"));
  box.append(el("p", null, d.urgent_confident
    ? "위급한 상황으로 보입니다. 이 신청은 접수하지 않았습니다. "
      + "119 또는 담당 사회복지사에게 지금 바로 연락해 주세요."
    : "남기신 내용만으로는 상황을 알기 어렵습니다. 접수하지 않고 담당자에게 전달했습니다. "
      + "급한 상황이면 담당자에게 직접 연락해 주세요."));
  box.append(el("p", "small", "동행고리 AI는 응급 여부를 판단하지 않습니다."));
  return box;
}

function renderReceipt(d) {
  const c = d.card;
  const frag = document.createDocumentFragment();

  frag.append(el("div", "receipt-title", "신청이 접수되었습니다"));
  frag.append(el("p", "lead",
    "담당 사회복지사가 확인한 뒤 연락드립니다. 아래 내용은 아직 확정된 일정이 아닙니다."));

  const dl = el("dl", "receipt");
  const put = (k, v) => { dl.append(el("dt", null, k)); dl.append(el("dd", null, v)); };

  // 대상자는 후보일 뿐이다. 확정된 것처럼 보이지 않게 적는다.
  const cand = c.target_candidates || [];
  put("어르신", cand.length === 1 ? `${cand[0].name} 님 (확인 예정)`
      : cand.length > 1 ? `${cand.length}분 중 확인 예정`
      : "담당자가 확인 예정");

  const f = c.fields || {};
  put("방문 예정일", f.date?.value ? `${f.date.value}${f.date.spoken ? ` (${f.date.spoken})` : ""}`
      : "담당자가 확인 후 안내");
  put("시각", f.time?.value || (f.time?.spoken ? `${f.time.spoken} — 오전·오후 확인 예정` : "미정"));
  put("병원", f.hospital?.value ? `${f.hospital.value}${f.hospital.status === "확인됨" ? "" : " (확인 예정)"}`
      : "담당자가 확인 후 안내");
  put("진료과", f.dept?.value || "확인 예정");
  frag.append(dl);

  frag.append(el("div", "quote", `남기신 내용: “${c.raw_utterance}”`));

  // 담당자가 물어볼 내용을 미리 알려주면 확인 전화가 짧아진다
  if (c.confirm_questions?.length) {
    const b = el("div", "ask");
    b.append(el("h3", null, "담당자가 확인할 내용"));
    const ul = el("ul");
    c.confirm_questions.forEach((q) => ul.append(el("li", null, q)));
    b.append(ul);
    frag.append(b);
  }

  frag.append(el("p", "small",
    "접수 번호 " + (d.intake_id ?? "—") + " · 확정은 담당 사회복지사가 합니다."));
  return frag;
}

async function submit() {
  const out = $("result");
  const phone = $("phone").value.trim();
  const text = $("utterance").value.trim();
  if (!phone || !text) {
    out.className = "bad";
    out.textContent = "연락처와 요청 내용을 모두 입력해 주세요.";
    return;
  }
  out.className = "loading";
  out.textContent = "접수 중입니다…";
  $("btnSubmit").disabled = true;
  try {
    const d = await api.createIntake(phone, text, CHANNEL);
    out.className = "";
    out.replaceChildren(d.urgent ? renderUrgent(d) : renderReceipt(d));
  } catch (e) {
    out.className = "bad";
    out.textContent = "접수하지 못했습니다: " + e.message;
  } finally {
    $("btnSubmit").disabled = false;
  }
}

$("btnSubmit").onclick = submit;
