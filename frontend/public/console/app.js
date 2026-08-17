// 동행고리 AI — 사회복지사 콘솔 (디자인 목업 반영판)
//
// 기존 /staff/ 와 **별도 경로**로 둔다. 본선 시연이 그 화면으로 돌아가고 있어서,
// 다 만들어 확인하기 전에는 바꾸지 않는다.
//
// 구조: state 하나 → 화면마다 render(state) 하나. 바뀌면 통째로 다시 그린다.
// 부분 갱신을 하지 않는 이유는 화면이 여섯이라, 어디를 언제 고쳐야 하는지를
// 사람이 추적하기 시작하면 반드시 한 군데를 빠뜨리기 때문이다.

import { api, session } from "../api.js";
import { el } from "./ui.js";
import { renderHome } from "./screens/home.js";
import { pendingIntakes, renderRequests } from "./screens/requests.js";
import { renderSchedule } from "./screens/schedule.js";
import { renderElders } from "./screens/elders.js";
import { renderRecords } from "./screens/records.js";
import { renderSettings } from "./screens/settings.js";
import { renderNewIntake } from "./screens/newintake.js";
import { loadAudit, renderAudit } from "./screens/audit.js";

const $ = (id) => document.getElementById(id);

// ── 상태 ────────────────────────────────────────────────────
//
// 화면 전체가 이 객체 하나만 본다. 서버에서 받은 것은 여기에만 담고,
// DOM 에는 아무것도 저장하지 않는다(예전엔 선택된 행을 DOM 클래스로만 알았다).
export const state = {
  screen: "home",
  user: null,
  loading: false,
  error: null,

  dashboard: null,          // { counts, intakes }
  intakes: [],
  selectedIntake: null,     // 상세를 펼친 접수 id
  intakeDetail: null,       // GET /api/intakes/{id} 응답
  requestFilter: "전체",

  profiles: [],
  profileQuery: "",
  selectedProfile: null,    // phone
  profileDetail: null,

  records: [],
  selectedRecord: null,
  recordFilter: "전체",
  recordDraft: false,        // 새 기록 쓰기 화면을 펼쳤나

  audit: [],

  scheduleFilter: "전체",
};

// 서버가 준 권한 목록으로 버튼을 가린다. 역할 이름을 여기 박지 않는다 —
// 박으면 권한표가 서버와 화면 두 곳이 되고, 하나만 고치면 어긋난다.
export const can = (perm) => (state.user?.permissions || []).includes(perm);

const SCREENS = {
  home: renderHome,
  requests: renderRequests,
  schedule: renderSchedule,
  elders: renderElders,
  records: renderRecords,
  newintake: renderNewIntake,
  audit: renderAudit,
  settings: renderSettings,
};

// ── 렌더 ────────────────────────────────────────────────────
export function render() {
  const main = $("main");
  main.replaceChildren();
  try {
    main.append(SCREENS[state.screen]());
  } catch (e) {
    // 한 화면이 터져도 콘솔 전체가 백지가 되지는 않게 한다. 시연 중에
    // 하얀 화면만큼 손쓸 수 없는 것이 없다.
    console.error("[console] 렌더 실패", state.screen, e);
    main.append(el("div", "wide-pane", [
      el("div", "err", `화면을 그리지 못했습니다 — ${e.message}`),
    ]));
  }
  for (const b of document.querySelectorAll(".nav-item")) {
    b.classList.toggle("on", b.dataset.screen === state.screen);
  }
  // 배지는 목록과 **같은 함수**로 센다. 예전엔 여기서 counts.waiting 을 읽었는데
  // 그건 status='접수 대기' 만 세어서, 임시 접수와 긴급이 빠진 숫자가 떴다.
  $("navReqN").textContent = pendingIntakes(state.intakes).length || "";
  $("navRecN").textContent = state.records.filter((r) => !r.approved).length || "";
}

/** 화면을 바꾸고 필요한 데이터를 받아 온다 */
export async function go(screen) {
  state.screen = screen;
  state.error = null;
  render();
  await load(screen);
  render();
}

/** state 를 고치고 다시 그린다 — 화면 코드가 부르는 유일한 갱신 경로 */
export function update(patch) {
  Object.assign(state, patch);
  render();
}

// ── 데이터 ──────────────────────────────────────────────────
async function load(screen) {
  state.loading = true;
  try {
    // 대시보드는 카운트와 접수 목록을 함께 준다. 화면마다 따로 담으면
    // 어떤 화면에서는 intakes 가 비어 네비 배지가 사라진다 — 실제로 그랬다.
    // 접수를 쓰는 화면은 전부 여기서 한 번에 채운다.
    if (["home", "requests", "schedule"].includes(screen)) {
      state.dashboard = await api.dashboard();
      state.intakes = state.dashboard.intakes || [];
      if (screen === "home") state.records = await api.postRecords().catch(() => []);
    } else if (screen === "elders") {
      state.profiles = await api.profiles(state.profileQuery);
    } else if (screen === "records") {
      state.records = await api.postRecords();
      // 새 기록은 확정된 접수에 붙는다 — 고를 목록이 있어야 한다
      state.intakes = (await api.dashboard()).intakes || [];
    } else if (screen === "audit") {
      await loadAudit();
    }
  } catch (e) {
    state.error = e;
  } finally {
    state.loading = false;
  }
}

/** 현재 화면의 데이터를 다시 받아 온다 */
export async function reload() {
  await load(state.screen);
  render();
}

/** 접수 상세를 펼친다 */
export async function openIntake(id) {
  state.selectedIntake = id;
  state.intakeDetail = null;
  render();
  try {
    state.intakeDetail = await api.getIntake(id);
  } catch (e) {
    state.error = e;
  }
  render();
}

/** 어르신 상세를 펼친다 */
export async function openProfile(phone) {
  state.selectedProfile = phone;
  state.profileDetail = null;
  render();
  try {
    state.profileDetail = await api.profile(phone);
  } catch (e) {
    state.error = e;
  }
  render();
}

// ── 자동 갱신 ───────────────────────────────────────────────
//
// 전화로 들어온 접수는 화면이 아니라 서버에서 생긴다. 갱신이 없으면 복지사가
// 화면을 다시 열 때까지 새 접수가 안 보인다 — 전화가 입구인 서비스에서 그건
// 접수를 놓치는 것과 같다.
//
// 도는 조건을 좁게 잡는다. 목록을 보는 화면이고, 상세를 펼쳐 놓지 않았고,
// 브라우저 탭이 앞에 있을 때만. 상세를 보는 중에 목록이 새로 그려지면
// 읽던 내용이 눈앞에서 바뀐다.
const POLL_MS = 3000;
let pollTimer = null;

function pollable() {
  return ["home", "requests", "schedule"].includes(state.screen)
    && !state.selectedIntake
    && document.visibilityState === "visible";
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => { if (pollable()) reload(); }, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

// 탭을 뒤로 보냈다 돌아오면 그 사이 쌓인 것을 즉시 반영한다.
document.addEventListener("visibilitychange", () => { if (pollable()) reload(); });

// ── 로그인 ──────────────────────────────────────────────────
function showApp(user) {
  state.user = user;
  $("view-login").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("whoami").textContent = user ? `${user.name}\n${user.role}` : "";
  // 권한이 없는 메뉴는 감춘다. 눌러 봐야 403 을 받는 화면을 보여줄 이유가 없다.
  // **가리는 것은 안내일 뿐 경계가 아니다** — 실제 차단은 서버가 한다.
  for (const b of document.querySelectorAll(".nav-item[data-perm]")) {
    b.classList.toggle("hidden", !can(b.dataset.perm));
  }
  startPolling();
  go("home");
}

function showLogin(message) {
  // 세션이 끊겨 여기로 온 것일 수 있다. 폴링을 세우지 않으면 3초마다 401 을
  // 받아 로그인 화면을 계속 다시 그린다.
  stopPolling();
  state.user = null;
  $("app").classList.add("hidden");
  $("view-login").classList.remove("hidden");
  const err = $("loginError");
  if (message) { err.textContent = message; err.classList.remove("hidden"); }
  else { err.classList.add("hidden"); }
  $("loginPassword").value = "";
}

session.onUnauthorized(() => showLogin("세션이 만료되었습니다. 다시 로그인해 주세요."));

async function doLogin() {
  const userId = $("loginId").value.trim();
  const password = $("loginPassword").value;
  if (!userId || !password) return showLogin("아이디와 비밀번호를 입력해 주세요.");
  $("btnLogin").disabled = true;
  try {
    const d = await api.login(userId, password);
    session.save(d.token, d.user);
    showApp(d.user);
  } catch (e) {
    showLogin(e.message);
  } finally {
    $("btnLogin").disabled = false;
  }
}

$("btnLogin").onclick = doLogin;
for (const id of ["loginId", "loginPassword"]) {
  $(id).onkeydown = (e) => { if (e.key === "Enter") doLogin(); };
}
$("btnLogout").onclick = async () => {
  try { await api.logout(); } catch { /* 이미 끊겼어도 화면은 되돌린다 */ }
  session.clear();
  showLogin();
};

for (const b of document.querySelectorAll(".nav-item")) {
  b.onclick = () => go(b.dataset.screen);
}

// 새로고침해도 토큰이 살아 있으면 로그인 화면을 거치지 않는다.
(async () => {
  if (!session.token) return showLogin();
  try {
    showApp(await api.me());
  } catch {
    showLogin();
  }
})();
