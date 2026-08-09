# 동행고리 AI — 프론트엔드 이미지
#
# 2단계 빌드: node 로 빌드 → nginx 로 정적 서빙.
# Vite / CRA / Next(static export) 는 이대로 동작한다. 빌드 산출물 디렉터리만
# 프로젝트에 맞게 BUILD_DIR 로 바꾸면 된다(Vite=dist, CRA·Next export=build/out).
#
# Next.js를 SSR로 돌릴 거면 이 파일 대신 node 런타임 이미지를 써야 한다 —
# 그 경우 frontend/README.md 의 안내를 참고할 것.
#
# package.json 이 없으면(아직 프론트 코드가 없으면) 빌드 단계를 건너뛰고
# public/ 을 그대로 서빙한다. 지금은 그 상태로 배선만 확인할 수 있다.

FROM node:22-alpine AS build
WORKDIR /src
COPY . .
ARG BUILD_DIR=dist
RUN if [ -f package.json ]; then \
        npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund; \
        npm run build; \
        mkdir -p /out && cp -r "${BUILD_DIR}"/. /out/; \
    else \
        echo "package.json 없음 — public/ 을 그대로 사용합니다"; \
        mkdir -p /out && cp -r public/. /out/; \
    fi

FROM nginx:alpine
COPY --from=build /out /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
