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

// channel 은 더 이상 여기서 정하지 않는다 — 서버가 '앱·웹(보호자)' 로 고정한다.
// 클라이언트가 고를 수 있으면 '전화' 로 보내 대리 접수 처리를 우회할 수 있었다.

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

// 접수증 — **보호자가 스스로 적어 보낸 것만** 되비춘다.
//
// 예전에는 접수카드(대상자 이름·병원 후보·확인 질문)를 그대로 보여줬다.
// 그런데 이 화면은 로그인이 없고 전화번호는 아무나 적을 수 있어서, 그 값들이
// 곧 조회 결과가 된다 — 번호만 바꿔 부르면 등록된 어르신의 이름과 진료 이력이
// 나왔다. 서버가 그 값들을 더 이상 내려주지 않으므로 화면도 같이 좁힌다.
//
// 어르신 이름·병원을 안 적는 게 불친절해 보이지만, 여기서 이름을 확인해 주는
// 순간 "이 번호에 누가 등록돼 있는지" 를 확인해 주는 API 가 된다.
function renderReceipt(d) {
  const frag = document.createDocumentFragment();

  frag.append(el("div", "receipt-title", "신청이 접수되었습니다"));
  frag.append(el("p", "lead",
    "담당 사회복지사가 확인한 뒤 연락드립니다. 아직 확정된 일정이 아닙니다."));

  const dl = el("dl", "receipt");
  const put = (k, v) => { dl.append(el("dt", null, k)); dl.append(el("dd", null, v)); };

  // 날짜·진료과는 보호자가 쓴 문장에서 뽑은 것이라 되비춰도 새로 알려주는 게 없다
  const date = d.date || {};
  put("방문 예정일", date.date
      ? `${date.date}${date.label ? ` (${date.label})` : ""}`
      : (date.label ? `${date.label} — 담당자가 확인 후 안내` : "담당자가 확인 후 안내"));
  put("진료과", d.dept || "담당자가 확인 예정");
  put("어르신·병원", "담당자가 확인 후 안내");
  frag.append(dl);

  frag.append(el("div", "quote", `남기신 내용: “${d.raw_utterance || ""}”`));

  // **신청번호를 보여줘야 한다.** 조회는 이 번호로만 되고, 서버가 접수 직후
  // 이 응답에서 한 번만 알려준다. 예전에는 intake_id(내부 번호)만 띄워서,
  // 이 화면으로 신청한 사람은 창을 닫으면 진행 상황을 볼 방법이 없었다.
  if (d.access_code) {
    const box = el("div", "notice");
    box.style.cssText = "background:#fff8e6;color:#7a5f10;border:1px solid #f2e2b8";
    box.append(el("div", null, "신청번호 " + d.access_code));
    box.append(el("div", "small",
      "진행 상황을 확인하실 때 필요합니다. 이 번호와 위에 적으신 연락처가 "
      + "둘 다 있어야 열리니, 적어 두시거나 화면을 저장해 주세요."));
    frag.append(box);
  }

  frag.append(el("p", "small", "확정은 담당 사회복지사가 합니다."));
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
    const d = await api.guardianIntake(phone, text);
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
