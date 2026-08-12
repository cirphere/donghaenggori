// 백엔드 호출을 한곳에 모은다.
//
// 화면을 제대로 만들 때 이 파일만 그대로 가져가면 배선이 끝난다 —
// 그래서 UI 코드와 섞지 않았다. 응답 모양은 docs/FRONTEND.md 가 계약이다.
//
// 같은 오리진으로 부른다. nginx 가 /api/ 를 백엔드(app:8000)로 넘기므로
// 주소를 따로 설정할 필요가 없고, CORS 도 걸리지 않는다.

const BASE = "";

async function req(path, { method = "GET", body, form } = {}) {
  const opt = { method };
  if (form) {
    opt.body = form;                       // multipart — Content-Type 은 브라우저가 붙인다
  } else if (body !== undefined) {
    opt.headers = { "Content-Type": "application/json" };
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, opt);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    // FastAPI 는 오류를 {detail: "..."} 로 준다
    const msg = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

export const api = {
  status:    ()                  => req("/api/status"),
  dashboard: ()                  => req("/api/dashboard"),
  warmup:    ()                  => req("/api/warmup", { method: "POST" }),

  // 화면 02 → 03. 긴급이면 card 가 null 로 온다(카드를 만들지 않는다).
  createIntake: (phone, utterance, channel = "전화", save = true) =>
    req("/api/intakes", { method: "POST", body: { phone, utterance, channel, save } }),

  // 음성 → 텍스트만
  stt: (file) => {
    const fd = new FormData();
    fd.append("file", file, file.name || "rec.webm");
    return req("/api/stt", { method: "POST", form: fd });
  },

  // 음성 하나로 접수까지 (시연 첫 장면). 응답에 stt 결과가 함께 들어온다.
  intakeFromAudio: (file, phone, channel = "전화") => {
    const fd = new FormData();
    fd.append("file", file, file.name || "rec.webm");
    const q = `?phone=${encodeURIComponent(phone)}&channel=${encodeURIComponent(channel)}`;
    return req("/api/intakes/from-audio" + q, { method: "POST", form: fd });
  },

  confirmIntake: (id, { hospital, date, level, actor, role }) =>
    req(`/api/intakes/${id}/confirm`,
        { method: "POST", body: { hospital, date, level, actor, role } }),

  createPostRecord: (intakeId, phone, memo, dept, target) =>
    req("/api/post-records",
        { method: "POST", body: { intake_id: intakeId, phone, memo, dept, target } }),

  // changed:false 면 이미 같은 상태였다는 뜻 — 오류가 아니다(더블클릭·재요청).
  approvePostRecord: (id, approved, role = "사회복지사") =>
    req(`/api/post-records/${id}/approve`, { method: "POST", body: { approved, role } }),

  audit: (limit = 20) => req(`/api/audit?limit=${limit}&role=사회복지사`),
};
