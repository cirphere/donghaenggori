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

**하드코딩하지 말 것.** 실행 시점에 결정된다.

```js
const API = window.API_BASE;      // public/index.html 에서 주입
fetch(`${API}/api/status`)
```

| 환경 | 주소 |
|---|---|
| 로컬 | `http://localhost:8000` |
| 배포 | `https://api.dohyeongops.com` |

프로젝트를 만들고 나면 이 주입 로직을 `index.html`이나 환경변수(`VITE_API_BASE` 등)로
옮겨도 된다. 중요한 건 **빌드 산출물에 주소가 박히지 않는 것**이다 — 그러면 로컬과
배포에서 같은 이미지를 못 쓴다.

CORS는 백엔드에서 열어두었으니 별도 설정이 필요 없다.

## 배포

```bash
docker compose -f docker-compose.yml -f docker-compose.frontend.yml up -d --build
```

프론트는 `localhost:3000`, 백엔드는 `localhost:8000`으로 뜬다.

API 문서와 화면별 연동 규칙은 [`../docs/FRONTEND.md`](../docs/FRONTEND.md)에 있다.
**특히 "UI가 반드시 지켜야 할 규칙" 절은 읽고 시작할 것** — 화면에서 어기면
기획 의도가 깨지는 항목들이다.
