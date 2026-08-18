# syntax=docker/dockerfile:1
# 동행고리 AI — 배포 이미지
#
# 배포 대상이 정해지지 않았으므로 amd64/arm64 양쪽에서 그대로 빌드된다.
# (AWS t3 = amd64, Oracle Ampere = arm64. 빌드하는 머신의 아키텍처를 따라간다)
# 의존성은 전부 두 아키텍처 휠이 있는 것으로 확인했다.
#
# 모델은 이미지에 굽지 않는다 — HF 캐시 1.5GB를 넣으면 이미지가 2GB를 넘고
# 아키텍처마다 다시 밀어야 한다. 첫 기동 때 볼륨으로 받는다(3일 전 기동 전제).

FROM python:3.12-slim

# ctranslate2·onnxruntime가 libgomp를 링크한다. slim 이미지에는 없다.
# curl은 헬스체크용.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch는 CPU 전용 빌드로 먼저 깐다. PyPI 기본 휠은 CUDA 런타임을 딸고 오는데
# (nvidia 2.9GB + triton 652MB) CPU 배포에서는 한 줄도 쓰이지 않는다.
# 이 한 줄로 이미지가 8.9GB → 3GB대가 된다. GPU 서버에 올릴 땐 이 줄을 빼면 된다.
#
# **requirements.txt 를 복사하기 전에** 깐다. 전에는 COPY 가 앞에 있어서,
# torch 와 아무 상관 없는 의존성 한 줄만 고쳐도 같은 레이어에 묶인 torch 를
# 통째로 다시 받았다. 이 순서면 torch 레이어는 베이스 이미지에만 의존한다.
#
# 캐시 마운트를 쓰는 이유: --no-cache-dir 은 레이어가 한 번 깨지면 무조건
# 네트워크에서 다시 받는다. 빌드 캐시가 정리되거나(WSL 디스크), 베이스 이미지
# 다이제스트가 바뀌거나, 빌드가 중간에 죽으면 매번 수 GB 재다운로드다.
# 캐시 마운트는 **이미지에 들어가지 않으므로** 이미지 크기는 그대로다.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu torch

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY donghaenggori ./donghaenggori
COPY tests ./tests
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 쓰기가 필요한 경로는 전부 볼륨으로 뺀다 — 컨테이너를 지워도 남는다
ENV HF_HOME=/models/hf \
    DONGHAENGGORI_DB=/data/donghaenggori.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 루트로 돌리지 않는다
RUN useradd -m -u 10001 app \
    && mkdir -p /models/hf /data \
    && chown -R app:app /app /models /data
USER app

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
# 워커는 1개다. 모델이 프로세스별로 적재되므로 2개면 4GiB 장비에서 OOM 난다.
# --proxy-headers: nginx 가 넘긴 X-Forwarded-Proto 를 믿는다. 없으면 앱이
# 자기가 http 로 서비스되는 줄 알고, 콜백 주소를 http 로 만들어 준다.
# 이 포트는 루프백과 도커 네트워크에만 열려 있어 nginx 외에는 닿지 않는다.
CMD ["uvicorn", "donghaenggori.web.api:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
