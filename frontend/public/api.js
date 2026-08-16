// 백엔드 호출을 한곳에 모은다.
//
// 화면을 제대로 만들 때 이 파일만 그대로 가져가면 배선이 끝난다 —
// 그래서 UI 코드와 섞지 않았다. 응답 모양은 docs/FRONTEND.md 가 계약이다.
//
// 같은 오리진으로 부른다. nginx 가 /api/ 를 백엔드(app:8000)로 넘기므로
// 주소를 따로 설정할 필요가 없고, CORS 도 걸리지 않는다.

const BASE = "";

// 오류를 문자열로만 던지면 확정 게이트(409)의 '막은 항목' 목록이 사라진다.
// 화면이 그걸 그려야 하므로 status 와 detail 을 그대로 들고 간다.
export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// ── 세션 ────────────────────────────────────────────────────
//
// **sessionStorage 에 둔다. localStorage 가 아니다.**
// 복지관 공용 PC 에서 쓰는 화면이라, 탭을 닫으면 토큰이 사라지는 편이 맞다.
// localStorage 는 브라우저를 껐다 켜도 남고 같은 오리진의 모든 탭이 공유한다 —
// 다음 사람이 앉으면 그대로 로그인된 상태가 된다.
//
// 토큰이 JS 에서 읽히는 건 이 구조의 한계다(HttpOnly 쿠키가 아니다). 대신 화면에
// 외부 스크립트를 하나도 싣지 않아 XSS 표면이 좁고, 토큰은 12시간이면 만료된다.
const TOKEN_KEY = "donghaenggori.token";
const USER_KEY = "donghaenggori.user";

let onUnauthorized = null;      // 401 을 만나면 부를 콜백 — 화면이 등록한다

export const session = {
  get token() { return sessionStorage.getItem(TOKEN_KEY); },
  get user() {
    try { return JSON.parse(sessionStorage.getItem(USER_KEY) || "null"); } catch { return null; }
  },
  save(token, user) {
    sessionStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(USER_KEY, JSON.stringify(user || null));
  },
  clear() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
  },
  // 세션이 끊겼을 때 화면을 로그인으로 되돌리는 훅
  onUnauthorized(fn) { onUnauthorized = fn; },
};

async function req(path, { method = "GET", body, form, auth = true } = {}) {
  const opt = { method, headers: {} };
  // 보호자 경로처럼 auth:false 로 부르는 곳은 헤더를 붙이지 않는다.
  // 토큰이 없을 때도 그냥 보낸다 — 서버가 401 을 주면 아래에서 로그인으로 되돌린다.
  if (auth && session.token) opt.headers.Authorization = `Bearer ${session.token}`;
  if (form) {
    opt.body = form;                       // multipart — Content-Type 은 브라우저가 붙인다
  } else if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, opt);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    // 401 은 '이 요청이 틀렸다' 가 아니라 '세션이 끝났다' 다. 화면마다 따로
    // 처리하면 어떤 버튼은 로그인으로 돌아가고 어떤 버튼은 빨간 글씨만 뜬다.
    // 로그인 요청 자체의 401(비밀번호 틀림)은 여기 해당하지 않는다.
    if (res.status === 401 && auth && !path.startsWith("/api/auth/login")) {
      session.clear();
      if (onUnauthorized) onUnauthorized();
    }
    // FastAPI 는 오류를 {detail: ...} 로 준다. detail 이 객체인 경우가 있어서
    // (확정 게이트) 메시지만 뽑아 쓰고 원본은 detail 로 넘긴다.
    const d = data && (data.detail ?? data.error);
    const msg = (typeof d === "string" && d)
      || (d && typeof d === "object" && d.message)
      || `HTTP ${res.status}`;
    throw new ApiError(msg, res.status, d);
  }
  return data;
}

export const api = {
  // ── 인증 ──────────────────────────────────────────────────
  // 로그인만 토큰 없이 부른다(auth:false). 여기서 나는 401 은 '비밀번호가
  // 틀렸다' 라서, 세션 만료 처리로 넘기면 안 된다.
  login: (email, password) =>
    req("/api/auth/login", { method: "POST", body: { email, password }, auth: false }),
  logout: () => req("/api/auth/logout", { method: "POST" }),
  me:     () => req("/api/auth/me"),

  status:    ()                  => req("/api/status"),
  dashboard: ()                  => req("/api/dashboard"),
  warmup:    ()                  => req("/api/warmup", { method: "POST" }),

  // 보호자 웹 전용 — 로그인 없이 부르는 유일한 쓰기 API.
  //
  // 직원용 createIntake 를 쓰면 안 된다. 그쪽은 로그인이 필요해서 401 이고,
  // 설령 열려 있어도 응답에 대상자 프로필과 진료 이력이 실려 나온다.
  // 이 경로의 응답은 **보호자가 적어 보낸 것만** 돌려준다(GuardianIntakeOut).
  // channel 은 서버가 '앱·웹(보호자)' 로 고정하므로 보내지 않는다.
  guardianIntake: (phone, utterance) =>
    req("/api/guardian/intakes", { method: "POST", body: { phone, utterance }, auth: false }),

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

  // 접수 상세. 접수 당시 만든 카드 전문(근거·확인질문 포함)이 card 에 들어 있다.
  // gate.allowed 가 false 면 확정 버튼을 잠그면 된다 — 판단은 서버가 한다.
  getIntake: (id) => req(`/api/intakes/${id}`),

  // 확인 필요가 남아 있으면 409 로 막힌다. ApiError.detail.gate.blockers 에
  // 무엇이 왜 막는지와 되물을 질문이 들어 있다.
  // 그래도 넘어가려면 acknowledge:true — 감사 로그에 '미확인 확정'으로 남는다.
  // actor·role 을 더 이상 보내지 않는다. 서버가 토큰에서 신원을 꺼내 쓰고,
  // 본문에 실어 보내도 무시한다. 예전엔 화면이 "김○○ 사회복지사" 라고 적어
  // 보냈는데, 그러면 감사 로그에 남는 사람과 화면이 말하는 사람이 달라진다.
  confirmIntake: (id, { hospital, date, level, acknowledge = false }) =>
    req(`/api/intakes/${id}/confirm`,
        { method: "POST", body: { hospital, date, level, acknowledge } }),

  // 통화로 확인한 값을 항목에 반영한다 — 게이트를 푸는 유일한 경로.
  // field: target | hospital | dept | date | time
  verifyField: (id, field, value) =>
    req(`/api/intakes/${id}/verify`, { method: "POST", body: { field, value } }),

  // 긴급 처리 완료 표시. changed:false 면 이미 처리됐다는 뜻 — 오류가 아니다.
  resolveUrgent: (id, note = "") =>
    req(`/api/intakes/${id}/resolve`, { method: "POST", body: { note } }),

  createPostRecord: (intakeId, phone, memo, dept, target) =>
    req("/api/post-records",
        { method: "POST", body: { intake_id: intakeId, phone, memo, dept, target } }),

  // changed:false 면 이미 같은 상태였다는 뜻 — 오류가 아니다(더블클릭·재요청).
  approvePostRecord: (id, approved) =>
    req(`/api/post-records/${id}/approve`, { method: "POST", body: { approved } }),

  audit: (limit = 20) => req(`/api/audit?limit=${limit}`),
};
