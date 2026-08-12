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

# 의존성을 먼저 깔아 레이어 캐시를 살린다 (torch가 커서 재빌드가 아프다)
COPY requirements.txt .
# torch는 CPU 전용 빌드로 먼저 깐다. PyPI 기본 휠은 CUDA 런타임을 딸고 오는데
# (nvidia 2.9GB + triton 652MB) CPU 배포에서는 한 줄도 쓰이지 않는다.
# 이 한 줄로 이미지가 8.9GB → 3GB대가 된다. GPU 서버에 올릴 땐 이 줄을 빼면 된다.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -r requirements.txt

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
