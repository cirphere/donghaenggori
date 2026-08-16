# 화면 접근 제한

**API 는 로그인 토큰이 지킨다.** nginx 기본 인증은 이제 그 위에 얹는 것이 아니라,
**토큰이 못 지키는 두 곳에만** 남아 있다 — `/docs`(Swagger)와 `/dev`(개발 UI).
둘 다 API 가 아니라 정적 문서·화면이라 401 이 붙지 않는다.

## 지금 무엇이 무엇을 지키나

| 경로 | 공개 여부 | 무엇이 지키나 |
|---|---|---|
| `/` 보호자 신청 | **공개** | 응답 범위 — `GuardianIntakeOut` 이 보호자가 적어 보낸 것만 돌려준다 |
| `/staff` 콘솔 화면 | **공개** | 열면 로그인 폼. 그 너머는 토큰 |
| `/api/*` 직원용 | 토큰 필요 | `Authorization: Bearer` 없으면 401 |
| `/api/reset` | 토큰 + 관리자 + 서버 로컬 | 셋 다 만족해야 한다 |
| `/api/guardian/intakes` | **무인증** | 응답 범위(위와 같음) |
| `/api/voice/` | **무인증** | 웹훅 서명(HMAC-SHA256) |
| `/api/health` | **무인증** | 상태만 돌려주고 데이터가 없다 |
| `/docs` `/dev` | **기본 인증** | 여기만 `STAFF_USER`/`STAFF_PASSWORD` |

실측으로 확인한 값이다. 기본 인증을 완전히 끈 상태에서 `/api/dashboard`
`/api/intakes` `/api/audit` `/api/status` `/api/reset` 전부 401 이고, 무인증인
보호자 접수 응답에도 프로필·이력이 없다.

## 왜 서버 전체에 걸지 않게 바꿨나

예전에는 서버 레벨에 기본 인증을 걸고 열어야 하는 곳마다 `auth_basic off` 로
구멍을 냈다. 백엔드에 인증이 없던 시절엔 유일한 방어선이었다.

문제는 **공개해야 할 경로가 생길 때마다 예외를 뚫어야 한다**는 것이다. 실제로
그걸 빠뜨려서 보호자 접수가 배포에서 통째로 동작하지 않았다 — nginx 가 먼저
막아 백엔드까지 닿지도 못했는데, 문서에는 `/` 가 "공개"로 적혀 있었다.

지금은 반대다. 기본이 공개이고 막을 곳만 막는다. 새 경로를 추가할 때 아무것도
안 해도 되고, 데이터는 토큰이 지킨다.

## 설정

```
STAFF_USER=donghaeng
STAFF_PASSWORD=<팀이 공유할 비밀번호>
```

비밀번호는 저장소에 들어가지 않는다. 컨테이너가 기동할 때 bcrypt 해시를 만들어
컨테이너 안에만 둔다(`frontend/docker-entrypoint.sh`).

**값을 안 넣으면 `/docs`·`/dev` 가 공개된다.** 접수 데이터에는 영향이 없다.

```
[frontend] 기본 인증 켜짐 (/docs·/dev 전용) — 사용자: donghaeng
[frontend] 알림: STAFF_USER/STAFF_PASSWORD 가 없어 /docs·/dev 가 공개됩니다.
```

## 화면 자체를 더 가리고 싶다면 — Cloudflare Access

`/staff` 로그인 폼조차 노출하지 않으려면 Access 를 쓴다. 무차별 대입은 앱
쪽에서 이미 막지만(계정당 5분 내 5회 실패 → 60초 잠금), 화면을 통째로 가리는
것은 nginx/Access 만 할 수 있다.

| | nginx 기본 인증 | Cloudflare Access |
|---|---|---|
| 팀원 준비물 | 공유 아이디·비번 | 이메일(계정 불필요, One-time PIN) |
| 로그인 | 브라우저 창에 즉시 | **메일로 코드를 받아** 입력 |
| 누가 들어왔는지 | 구분 안 됨 | 이메일로 구분됨 |

시연장에서 **이메일을 기다리는 것이 실제 위험**이라 지금은 쓰지 않는다.
심사 도중 "코드가 안 와요"가 되면 손쓸 방법이 없다.

### 설정 방법

**호스트 전체를 Access 뒤에 두는 것**이 가장 확실하다.

가장 확실하다. 시연·내부 사용만 할 때는 이걸 쓴다.

1. Cloudflare Zero Trust → **Access → Applications → Add an application**
2. Self-hosted 선택
3. Application domain: `donghaenggori.dohyeongops.com` (경로 비움 = 전체)
4. Policy: Allow → Emails → 팀 구성원 이메일

이러면 **보호자 웹도 함께 막힌다.** 보호자에게 링크를 열어줄 단계가 아니라면
이 구성이 맞다.

#### ⚠ 전화 연동을 붙이면 예외가 하나 필요하다

호스트 전체에 Access 를 걸면 **ClawOps 수신 웹훅도 막힌다.** ClawOps 는 브라우저가
아니라 로그인 화면을 통과하지 못하고, 전화가 걸려와도 우리 서버까지 오지 않는다.

회선을 붙일 때 `/api/voice` 만 Bypass 로 뺀다.

> Add an application (하나 더) → Application domain 은 같게, **Path: `/api/voice`**
> Policy: **Bypass — Everyone**

이 경로는 Access 대신 **웹훅 서명(HMAC-SHA256)** 이 지킨다. 서명이 없거나 틀리면
401, 키가 미설정이면 503 이다. 자세한 것은 `docs/전화연동.md` 참조.

회선이 아직 없다면 이 예외는 만들지 않는다 — 필요할 때 만든다.

#### 보호자 웹

**별도 조치 없이 공개해도 된다.** 이 화면이 부르는 건 `POST /api/guardian/intakes`
하나뿐이고, 응답은 보호자가 적어 보낸 것만 돌려준다(`GuardianIntakeOut`).
나머지 API 는 전부 토큰이 없으면 401 이다.

## 호스트를 나누고 싶을 때

터널에 Public Hostname 을 둘 만들면 화면이 갈린다. **보호자에게 준 링크로
직원 화면에 닿지 못한다.**

```
guardian.<도메인>  → 터널 → frontend:80
staff.<도메인>     → 터널 → frontend:80
```

Cloudflare Zero Trust → Networks → Tunnels → 해당 터널 → Public Hostname 에
두 줄을 추가하면 된다. Service 는 둘 다 `http://frontend:80`.

nginx 는 **이미 준비돼 있다.** 호스트 접두어로 판별한다:

| 경로 | 기존 호스트 | `guardian.*` | `staff.*` |
|---|---|---|---|
| `/` 보호자 신청 | 200 | 200 | **302 → /staff/** |
| `/staff` 콘솔 | 200 | **404** | 200 |
| `/docs` `/dev` | 기본 인증 | **404** | 기본 인증 |
| `/api/*` | 토큰 | 토큰 | 토큰 |

**도메인을 박지 않았다.** `guardian.` · `staff.` 로 시작하는지만 본다 — 도메인이
바뀌어도 그대로 동작하고, **호스트를 추가하기 전에는 지금과 완전히 같다.**
즉 이 설정을 배포해 두어도 Cloudflare 를 건드리기 전까지 아무것도 안 바뀐다.

`/staff` 를 403 이 아니라 404 로 돌려주는 것은 의도적이다 — 보호자 링크를 받은
사람에게 "여기 뭔가 있는데 막혔다" 를 알려줄 이유가 없다.

호스트를 나눈 뒤에는 **`staff.<도메인>` 에만 Cloudflare Access** 를 걸 수 있다.
그러면 보호자 링크는 열린 채로 직원 화면만 가려진다.

## 확인 방법

로그인 없이 데이터 API 가 열리는지 직접 확인한다.

```bash
# 401 이어야 한다. 200 이면 뚫린 것이다.
curl -s -o /dev/null -w '%{http_code}\n' https://<도메인>/api/dashboard
```

호스트를 나눴다면 갈리는지도 본다.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://guardian.<도메인>/staff/   # 404
curl -s -o /dev/null -w '%{http_code}\n' https://staff.<도메인>/            # 302
```

배포 점검 스크립트가 무인증 차단을 매번 확인한다 — **토큰 없이도 돈다.**

```bash
python -m tests.preflight --url https://<도메인>
```

## 현재 상태

- 화면 분리: **완료** (`/` 보호자, `/staff` 사회복지사)
- 호스트 분리: **nginx 는 준비됨** — Cloudflare 에 Public Hostname 둘을 추가하면 켜진다
- **백엔드 인증: 구현됨** — `/api/auth/login` 으로 토큰 발급(아이디+비밀번호).
  직원용 API 는 전부 `Authorization: Bearer` 없이는 401. 계정은
  `python -m donghaenggori.services.create_user` 로 운영자가 미리 만든다
  (사회복지사·동행매니저·**관리자** 셋).
- **nginx 기본 인증: `/docs`·`/dev` 전용** — 데이터는 토큰이 지키므로 서버
  전체에 걸 이유가 없다. 이 값이 없어도 접수 데이터는 안전하다.
- Access 정책: 선택 사항. `/staff` 로그인 폼조차 가리고 싶을 때만.
