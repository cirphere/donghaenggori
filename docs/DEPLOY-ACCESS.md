# 사회복지사 콘솔 접근 제한 (Cloudflare Access)

## 왜 필요한가

웹을 둘로 나눴다.

| 경로 | 화면 | 공개 여부 |
|---|---|---|
| `/` | 보호자 신청 | **공개** — 가족이 링크로 들어온다 |
| `/staff` | 사회복지사 콘솔 | **비공개** — 확정·승인·감사로그 |

문제는 백엔드가 **아직 요청 본문의 `role` 을 그대로 믿는다**는 것이다. 인증이
없어서, 화면을 나눠도 브라우저에서 이렇게 보내면 그만이다.

```bash
curl -X POST https://<도메인>/api/intakes/1/confirm \
  -H 'Content-Type: application/json' \
  -d '{"hospital":"임의","date":"2026-08-20","level":"단순 안내","role":"관리자"}'
```

즉 **`/staff` 화면을 감추는 것만으로는 아무것도 막지 못한다.** API 자체를 막아야
한다. 다행히 프론트와 API가 같은 오리진(nginx 한 대)이라, 호스트에 Access를 걸면
API 요청에도 같은 로그인 쿠키가 실린다.

## 지금 할 수 있는 것 — 호스트 전체를 Access 뒤에

가장 확실하다. 시연·내부 사용만 할 때는 이걸 쓴다.

1. Cloudflare Zero Trust → **Access → Applications → Add an application**
2. Self-hosted 선택
3. Application domain: `donghaenggori.dohyeongops.com` (경로 비움 = 전체)
4. Policy: Allow → Emails → 팀 구성원 이메일

이러면 **보호자 웹도 함께 막힌다.** 보호자에게 링크를 열어줄 단계가 아니라면
이 구성이 맞다.

## ⚠ 전화 연동을 붙이면 예외가 하나 필요하다

호스트 전체에 Access 를 걸면 **ClawOps 수신 웹훅도 막힌다.** ClawOps 는 브라우저가
아니라 로그인 화면을 통과하지 못하고, 전화가 걸려와도 우리 서버까지 오지 않는다.

회선을 붙일 때 `/api/voice` 만 Bypass 로 뺀다.

> Add an application (하나 더) → Application domain 은 같게, **Path: `/api/voice`**
> Policy: **Bypass — Everyone**

이 경로는 Access 대신 **웹훅 서명(HMAC-SHA256)** 이 지킨다. 서명이 없거나 틀리면
401, 키가 미설정이면 503 이다. 자세한 것은 `docs/전화연동.md` 참조.

회선이 아직 없다면 이 예외는 만들지 않는다 — 필요할 때 만든다.

## 보호자 웹을 공개해야 할 때

호스트명을 둘로 나누는 방법이 가장 깔끔하다. Access는 경로 단위 정책도 지원하지만,
`/api/` 를 공유하는 지금 구조에서는 **경로로 나눠도 API가 열려 있어 의미가 없다.**

```
guardian.dohyeongops.com  → 터널 → frontend:80   (공개)
staff.dohyeongops.com     → 터널 → frontend:80   (Access 필수)
```

Cloudflare Tunnel에 Public Hostname을 두 개 만들어 같은 `frontend:80` 으로 보내고,
`staff.` 쪽에만 Access 정책을 건다. 그래도 **API 는 여전히 두 호스트 모두에서
열린다** — 공개 호스트로 `role: "관리자"` 를 보내면 통과한다.

그래서 보호자 웹을 진짜로 공개하려면 다음 중 하나가 필요하다.

1. **백엔드 인증 구현** (권장, 본선 후) — 세션에서 역할을 꺼내고 요청 본문의
   `role`·`actor` 는 무시한다. `db.can()` 호출부는 그대로 두고 신원의 출처만 바꾸면 된다.
2. **쓰기 API 를 staff 호스트로만 노출** — nginx에서 `$host` 를 보고
   `/api/intakes/*/confirm`, `/api/post-records/*/approve`, `/api/audit` 를
   공개 호스트에서 403 처리. 임시방편이지만 인증 없이도 경계가 생긴다.

## 확인 방법

Access를 건 뒤 터널 주소로 접속하면 Cloudflare 로그인 화면이 먼저 떠야 한다.
로그인 없이 API가 열리는지도 직접 확인한다.

```bash
# 로그인 없이 → 302(로그인 리다이렉트) 또는 403 이어야 한다. 200 이면 뚫린 것이다.
curl -s -o /dev/null -w '%{http_code}\n' https://<도메인>/api/dashboard
```

## 현재 상태

- 화면 분리: **완료** (`/` 보호자, `/staff` 사회복지사)
- nginx 경로 분리: **완료**
- Access 정책: **사람이 대시보드에서 설정해야 함**
- 백엔드 인증: **미구현** — 본선 후 과제

`/api/reset` 만은 예외적으로 이미 막혀 있다. Cloudflare 헤더가 붙은 요청(=외부에서
터널을 거쳐 온 요청)을 403으로 거절한다. 다른 엔드포인트에는 그런 보호가 없다.
