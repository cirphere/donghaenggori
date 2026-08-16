# 화면 접근 제한

> ⚠️ **이제는 필수가 아니다.** 아래 A(nginx 기본 인증)는 백엔드에 로그인이 없던
> 시절엔 유일한 방어선이었지만, 지금은 API 자체가 로그인 토큰으로 스스로를
> 지킨다(`docs/FRONTEND.md`의 "인증" 절 참조). 보호자 웹(`/`)이 쓰는
> `POST /api/guardian/intakes` 딱 하나만 무인증이고, 나머지 API는 전부
> `Authorization: Bearer <token>` 없이는 401이다. 그래서 nginx 기본 인증은
> 이제 **추가 방어선(defense in depth)**이지 켜지 않아도 API가 뚫리진 않는다.
> 다만 `/staff` **정적 화면**(HTML/JS 자체)을 아예 못 열게 감추는 건 여전히
> nginx/Access만 할 수 있는 일이라, 시연장 등에서는 계속 켜두길 권장한다.

두 가지 길이 있다. **지금 쓰는 것은 A(nginx 기본 인증)** 다.

| | A. nginx 기본 인증 | B. Cloudflare Access |
|---|---|---|
| 팀원 준비물 | 공유 아이디·비번 | 이메일(계정 불필요, One-time PIN) |
| 로그인 | 브라우저 창에 즉시 | **메일로 코드를 받아** 입력 |
| 설정 위치 | 저장소(`nginx.conf`) + `.env` | Cloudflare 대시보드 |
| 누가 들어왔는지 | 구분 안 됨 | 이메일로 구분됨 |

시연장에서 **이메일을 기다리는 것이 실제 위험**이라 A를 택했다. 심사 도중
"코드가 안 와요"가 되면 손쓸 방법이 없다. 둘을 겹쳐 쓸 수도 있다.

---

## A. nginx 기본 인증 (현재)

`.env` 에 두 값을 넣고 `down → up` 하면 끝이다.

```
STAFF_USER=donghaeng
STAFF_PASSWORD=<팀이 공유할 비밀번호>
```

비밀번호는 저장소에 들어가지 않는다. 컨테이너가 기동할 때 bcrypt 해시를 만들어
컨테이너 안에만 둔다(`frontend/docker-entrypoint.sh`).

**인증에서 빠지는 곳은 둘뿐이고, 각자 다른 방식으로 지킨다.**

| 경로 | 왜 예외인가 | 무엇이 지키나 |
|---|---|---|
| `/api/voice/` | 통신사는 로그인 화면을 통과하지 못한다 | 웹훅 서명(HMAC-SHA256) |
| `/api/health` | 모니터링이 로그인할 수 없다 | 상태만 돌려주고 데이터가 없다 |

**값을 안 넣으면 인증이 꺼진다.** 조용히 열리지 않도록 기동 로그에 경고를 크게
남기니, 배포 후 `docker compose logs frontend | grep frontend` 로 확인할 것.

```
[frontend] 기본 인증 켜짐 — 사용자: donghaeng        ← 이게 나와야 한다
[frontend] 경고: STAFF_USER/STAFF_PASSWORD 가 없어 …  ← 이러면 열려 있다
```

---

## B. Cloudflare Access (선택)

## 왜 필요한가

웹을 둘로 나눴다.

| 경로 | 화면 | 공개 여부 |
|---|---|---|
| `/` | 보호자 신청 | **공개** — 가족이 링크로 들어온다 |
| `/staff` | 사회복지사 콘솔 | **비공개** — 확정·승인·감사로그 |

과거엔 백엔드가 요청 본문의 `role` 을 그대로 믿었다. 인증이 없던 시절엔 화면을
나눠도 브라우저에서 이렇게 보내면 그만이었다.

```bash
curl -X POST https://<도메인>/api/intakes/1/confirm \
  -H 'Content-Type: application/json' \
  -d '{"hospital":"임의","date":"2026-08-20","level":"단순 안내","role":"관리자"}'
```

**이제는 막힌다** — `role` 은 본문에서 더 이상 읽지 않고(보내도 무해, 조용히
무시됨), `Authorization: Bearer <token>` 이 없으면 401이 떨어진다. 아래 예시는
그 취약점이 왜 있었는지 기록으로 남겨둔다.

그래도 **`/staff` 화면을 감추는 것과 API를 막는 것은 별개 층**이다. 토큰이 API를
지키는 것과 별개로, Access는 화면 자체(그리고 `/staff` 로 향하는 트래픽)를 가려서
로그인 안 한 사람이 콘솔 UI를 아예 못 열게 한다. 다행히 프론트와 API가 같은
오리진(nginx 한 대)이라, 호스트에 Access를 걸면 API 요청에도 같은 로그인 쿠키가
실린다.

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

**이제는 별도 조치 없이 공개해도 된다.** 예전엔 API 전체가 요청 본문의 `role`을
믿었기 때문에, 보호자 웹을 열면 그 경로로 `role: "관리자"`를 실어 확정·감사로그까지
건드릴 수 있었다. 지금은 다르다:

- 보호자 웹이 실제로 부르는 건 `POST /api/guardian/intakes` **하나뿐**이고, 이건
  원래부터 무인증으로 설계됐다(`docs/FRONTEND.md` "1. 접수" 참조) — `phone`·
  `utterance`만 받고 `channel`은 서버가 강제로 고정한다. 여기로 할 수 있는 건
  "접수 신청 하나 만들기"뿐, 읽기는 전혀 없다.
- **응답도 좁혀 두었다**(`GuardianIntakeOut`). 예전에는 직원용 응답을 그대로
  돌려줘서, 남의 번호만 넣으면 이름·보호자 연락처·거동 상태·독거 여부·진료
  이력이 통째로 나왔다 — 쓰기 전용이라는 말이 응답까지 안전하다는 뜻은
  아니었다. 지금은 보호자가 적어 보낸 것만 돌려준다.
  `tests/test_guardian_privacy.py` 가 회귀로 잡는다.
- nginx 도 이 경로와 보호자 페이지 네 파일(`/`, `/index.html`, `/guardian.js`,
  `/style.css`, `/api.js`)만 기본 인증에서 뺀다. `/staff` 와 나머지 `/api/` 는
  그대로 막힌다 — `location =` 이 정규식 location 보다 먼저 매칭되는 성질을
  쓴 것이라, 파일을 추가할 때 같은 방식으로 한 줄씩 여는 것이 안전하다.
- 그 외 모든 쓰기·조회 API(`/api/intakes` 직원용, `/api/dashboard`, `/api/status`,
  `/api/facilities`, `confirm`/`verify`/`resolve`/`approve`/`audit` 등)는
  `Authorization: Bearer <token>`이 없으면 401이다. 보호자 웹의 JS는 애초에
  토큰을 가질 방법이 없으니(로그인 UI 자체가 없다) 이 경로들을 건드릴 수 없다.

그래도 호스트를 나누고 싶다면(터널 로그를 화면별로 나눠 보고 싶다든가) 아래처럼
할 수 있지만, **보안 목적으로는 더 이상 필수가 아니다.**

```
guardian.dohyeongops.com  → 터널 → frontend:80   (공개)
staff.dohyeongops.com     → 터널 → frontend:80   (Access 권장 — 화면 자체를 가림)
```

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
- **nginx 기본 인증: 완료** — `.env` 에 `STAFF_USER`/`STAFF_PASSWORD` 만 넣으면 켜진다
- Access 정책: 선택 사항. 쓰려면 대시보드에서 사람이 설정
- 백엔드 인증: **구현됨** — `/api/auth/login` 으로 토큰 발급, 확정·확인·처리·승인·
  감사조회는 `Authorization: Bearer` 없으면 401. 계정은
  `python -m donghaenggori.services.create_user` 로 운영자가 미리 만든다.
  nginx 기본 인증과는 별개 계층 — 하나는 화면(`/staff`) 전체를, 하나는 API
  개별 엔드포인트를 지킨다.

`/api/reset` 은 **두 겹**으로 막혀 있다 — 로그인(없으면 401)과 서버 로컬
제한(Cloudflare 헤더가 붙은 요청은 403). 접수·감사로그·이력·세션을 통째로
지우는 가장 파괴적인 호출이라 한 겹으로는 부족하다.

예전엔 CF 헤더 검사 하나뿐이었다. 실제 배포에서는 인터넷 트래픽이 전부 터널을
거쳐 헤더가 붙으니 막히긴 했지만, **nginx 기본 인증을 끄면 그 순간 삭제 버튼이
됐다** — 실제로 확인했다(기본 인증을 끈 채로 헤더 없이 부르면 `200 {"ok":true}`).
지금은 기본 인증을 꺼도 401 이다.

> 그래서 **nginx 기본 인증은 이제 정말 선택 사항**이다. 껐을 때 추가로 열리는
> 것은 `/staff` 로그인 화면·`/docs`·`/dev` 정적 자산뿐이고, 데이터 API 는 전부
> 401 이다(실측). 화면을 감추고 싶으면 켜고, 아니면 꺼도 데이터는 안전하다.
