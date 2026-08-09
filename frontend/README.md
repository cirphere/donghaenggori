# 프론트엔드

백엔드와 **별도 컨테이너**로 뜬다. 이 디렉터리에 프로젝트를 만들면 그대로 빌드된다.

```
frontend/
├── Dockerfile        건드릴 일 거의 없음
├── nginx.conf        SPA 라우팅 + 캐시 설정
├── public/           지금은 자리표시자 페이지. 프로젝트 만들면 대체됨
└── (여기에 React/Vue 프로젝트)
```

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
