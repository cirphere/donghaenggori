# 프론트엔드

백엔드와 **별도 컨테이너**로 뜬다. 이 디렉터리에 프로젝트를 만들면 그대로 빌드된다.

```
frontend/
├── Dockerfile        건드릴 일 거의 없음
├── nginx.conf        SPA 라우팅 + 캐시 설정
├── public/           임시 화면 (빌드 도구 없이 그대로 서빙된다)
│   ├── api.js        ★ 백엔드 호출 모음 — 이건 그대로 가져다 쓰면 된다
│   ├── index.html    보호자 웹 (공개)
│   ├── guardian.js
│   ├── style.css
│   └── staff/        사회복지사 콘솔 (Access 뒤에 둔다)
│       ├── index.html
│       └── staff.js
└── (여기에 React/Vue 프로젝트를 만들면 public/ 대신 그게 빌드된다)
```

## 지금 상태

`public/` 에 **임시 화면**이 있다. 계약(`card.fields`, `policy`, `urgent_confident`)을
그대로 그려서 백엔드 연결과 응답 모양을 눈으로 확인하는 용도다. 디자인은 최소한만
넣었고, 다음 규칙이 지켜지는지 보려고 만들었다.

- 상태는 `확인됨` / `추정` / `확인 필요` 3단계 — 확률(%)로 바꾸지 않는다
- 항목마다 근거(`evidence`)를 함께 띄운다
- 긴급이면 카드를 그리지 않는다. `urgent_confident` 로 배너 강도를 나눈다
  (빨강=긴급 단정 / 노랑=이해 못 함, 확인 필요)
- 오전·오후를 모르는 시각은 화면이 임의로 채우지 않는다
- 복지자원은 `관내` / `같은 시도` / `타 지역` 을 반드시 표시한다

**웹이 둘인 이유는 화면이 아니라 권한이다.** 보호자는 신청만 하고, 확정·승인·감사로그는
사회복지사 콘솔에서만 한다. 다만 백엔드가 아직 요청 본문의 `role` 을 그대로 믿으므로
화면 분리만으로는 아무것도 막지 못한다 — 실제 경계는 Cloudflare Access 다.
반드시 `docs/DEPLOY-ACCESS.md` 를 읽고 설정할 것.

**제대로 만들 때**: `api.js` 는 그대로 쓰고 나머지를 갈아끼우면 된다. 이 디렉터리에 `package.json` 이 생기는 순간 Dockerfile 이
빌드 경로로 자동 전환되므로(아래 참조) 배포 설정은 손댈 필요가 없다.

임시 화면을 로컬에서 볼 때는 정적 서버만으로는 `/api/` 가 404 다. nginx 를 띄우거나
`docker compose --profile frontend up` 으로 확인한다.

## 시작하기

```bash
cd frontend
npm create vite@latest . -- --template react-ts    # 예: Vite + React
npm install
npm run dev                                        # 로컬 개발 서버
```

로컬 개발 중에는 백엔드를 따로 띄워두면 된다.

```bash
cd ..
docker compose up -d app          # 백엔드만 → localhost:8000
```

## 빌드 산출물 경로

`Dockerfile`이 `package.json`을 감지하면 `npm run build`를 돌리고 결과를 nginx로 옮긴다.
산출물 디렉터리가 `dist`가 아니면 루트 `.env`에 적어준다.

| 도구 | 산출물 |
|---|---|
| Vite | `dist` (기본값) |
| CRA | `build` |
| Next (static export) | `out` |

```
FRONTEND_BUILD_DIR=build
```

> **Next.js를 SSR로 돌릴 거면** 이 Dockerfile은 맞지 않다. nginx 정적 서빙 대신
> node 런타임이 필요하다. 그 경우 말해달라 — Dockerfile을 바꿔주겠다.

## API 주소

**같은 오리진이다.** nginx가 `/api/`를 백엔드 컨테이너로 넘기므로 도메인을 붙일 필요가 없다.

```js
fetch('/api/status')      // 이렇게
```

빌드 산출물에 주소가 박히지 않으므로 로컬과 배포에서 같은 이미지를 쓸 수 있고,
CORS도 신경 쓸 필요가 없다.

`npm run dev`로 개발 서버를 띄우면 nginx를 거치지 않으니 proxy 설정을 해준다.

```js
// vite.config.ts
export default defineConfig({
  server: { proxy: { '/api': 'http://localhost:8000',
                     '/docs': 'http://localhost:8000' } },
})
```

이러면 개발 서버에서도 `/api/...` 상대 경로가 그대로 동작해 배포와 같아진다.

## 배포

```bash
docker compose -f docker-compose.yml -f docker-compose.frontend.yml up -d --build
```

프론트는 `localhost:3000`, 백엔드는 `localhost:8000`으로 뜬다.

API 문서와 화면별 연동 규칙은 [`../docs/FRONTEND.md`](../docs/FRONTEND.md)에 있다.
**특히 "UI가 반드시 지켜야 할 규칙" 절은 읽고 시작할 것** — 화면에서 어기면
기획 의도가 깨지는 항목들이다.
