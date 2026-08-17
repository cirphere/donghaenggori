// 새 접수 — 직원이 직접 접수를 만든다.
//
// 목업에는 없는 화면이다. 목업은 모든 요청이 전화로 들어온다고 보는데,
// 실제로는 두 가지 이유로 필요하다.
//
// 1. **시연의 대체 경로.** 장면 1의 실제 전화가 이 시연의 최대 무기이자 최대
//    위험이다. 회선·터널·STT 중 하나만 어긋나면 시작부터 막히므로, 녹음 파일과
//    텍스트 입력이라는 2·3순위를 반드시 준비한다(docs/시연대본.md 운영 메모).
// 2. **현장.** 복지사가 방문 중에 대신 접수하거나, 생활지원사가 전해 온 요청을
//    옮겨 적는 경로가 실제로 있다.

import { api } from "../../api.js";
import { button, el, sectionTitle } from "../ui.js";
import { openIntake, reload, update } from "../app.js";

// 시연 발화 모음 — 대본의 장면별 발화를 그대로 둔다. 시연장에서 오타 하나로
// 결과가 달라지면 곤란하다.
const SAMPLES = [
  ["010-1234-5678", "모레 정형외과 가야겄어. 저번에 무릎 봐준 데", "장면 1 · 단골 병원"],
  ["010-1234-5678", "내일 아니고 모레 3시에 정형외과 가야 해", "장면 2 · 시각이 모호"],
  ["010-7777-8888", "내일 그 큰 병원 좀 가야 쓰겄는디", "장면 4 · 이력 없음"],
  ["010-9876-5432", "우리 어매 병원 좀 델꼬 가야 쓰겄는디", "대리 접수"],
  ["010-4218-8885", "가슴이 답답하고 숨이 차", "장면 3 · 긴급"],
];

let recorder = null;
let chunks = [];

export function renderNewIntake() {
  const phone = el("input");
  phone.placeholder = "발신번호 (010-1234-5678)";
  phone.value = "010-1234-5678";
  phone.style.cssText = inputCss();

  const utter = el("textarea");
  utter.rows = 3;
  utter.placeholder = "어르신 발화를 그대로 적어 주세요";
  utter.style.cssText = inputCss() + "resize:vertical;";

  const status = el("div", "ask", "");

  const submit = button("접수카드 만들기", "btn primary", async () => {
    const p = phone.value.trim(), u = utter.value.trim();
    if (!p || !u) { status.textContent = "발신번호와 발화를 모두 적어 주세요."; return; }
    submit.disabled = true;
    status.textContent = "분석 중…";
    try {
      const r = await api.createIntake(p, u, "직접(기관)");
      await afterCreate(r, status);
    } catch (e) {
      status.textContent = "실패 — " + e.message;
    } finally {
      submit.disabled = false;
    }
  });

  return el("div", "wide-pane", [
    el("h1", "home-h1", "새 접수"),
    el("div", "home-sub",
       "전화로 들어오지 않은 요청을 직접 접수합니다. 발화를 그대로 적으면 "
       + "접수카드가 만들어져요."),

    sectionTitle("발화 입력"),
    el("div", "frow", [el("div", "lb", "발신번호"), el("div", "vl", [phone])]),
    el("div", "frow", [el("div", "lb", "발화"), el("div", "vl", [utter])]),

    sectionTitle("시연 발화 모음", "누르면 위 칸이 채워져요"),
    el("div", "chips", SAMPLES.map(([p, t, label]) => {
      const c = el("button", "chip", label);
      c.onclick = () => { phone.value = p; utter.value = t; status.textContent = ""; };
      return c;
    })),

    sectionTitle("음성으로 접수", "녹음하거나 파일을 올리면 받아쓰기부터 합니다"),
    audioRow(phone, status),

    el("div", "footbar", [status, el("div", "grow"), submit]),
  ]);
}

function audioRow(phone, status) {
  const file = el("input");
  file.type = "file";
  file.accept = "audio/*";
  file.style.display = "none";
  file.onchange = () => {
    if (file.files[0]) sendAudio(file.files[0], phone.value.trim(), status);
  };

  const pick = button("음성 파일 올리기", "btn", () => file.click());
  const rec = button("녹음 시작", "btn", async () => {
    if (recorder && recorder.state === "recording") {
      recorder.stop();
      rec.textContent = "녹음 시작";
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => chunks.push(e.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks, { type: "audio/webm" });
        sendAudio(new File([blob], "rec.webm", { type: "audio/webm" }),
                  phone.value.trim(), status);
      };
      recorder.start();
      rec.textContent = "녹음 중지";
      status.textContent = "녹음 중…";
    } catch (e) {
      // 마이크 권한이 없거나 https 가 아니면 여기로 온다. 시연장에서 이걸
      // 만나면 파일 업로드나 텍스트로 넘어가면 된다.
      status.textContent = "마이크를 쓸 수 없어요 — 파일 올리기나 텍스트를 쓰세요. " + e.message;
    }
  });

  return el("div", "row-sub", [rec, document.createTextNode(" "), pick, file]);
}

async function sendAudio(f, phone, status) {
  if (!phone) { status.textContent = "발신번호를 먼저 적어 주세요."; return; }
  status.textContent = "받아쓰는 중… (첫 요청은 모델을 올리느라 오래 걸려요)";
  try {
    const r = await api.intakeFromAudio(f, phone, "전화");
    await afterCreate(r, status);
  } catch (e) {
    status.textContent = "실패 — " + e.message;
  }
}

/** 접수가 만들어지면 요청 화면으로 데려간다 — 만들어 놓고 어디 갔는지
 *  못 찾으면 화면을 만든 의미가 없다. */
async function afterCreate(r, status) {
  status.textContent = r.urgent
    ? "긴급으로 판정되어 접수카드를 만들지 않았습니다 — 요청 화면에서 확인하세요."
    : "접수카드를 만들었습니다.";
  update({ screen: "requests" });
  await reload();
  if (r.intake_id) await openIntake(r.intake_id);
}

function inputCss() {
  return "width:100%;padding:9px 11px;border:1px solid var(--line);"
       + "border-radius:8px;background:var(--white);";
}
