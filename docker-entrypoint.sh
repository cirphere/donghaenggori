#!/bin/sh
# 첫 기동에만 시드를 넣는다. DB가 볼륨에 남아 있으면 건드리지 않는다.
set -e

DB="${DONGHAENGGORI_DB:-/data/donghaenggori.db}"

if [ ! -f "$DB" ]; then
    echo "[entrypoint] DB 없음 → 시드 생성 ($DB)"
    python -m donghaenggori.services.seed

    # 공공데이터 적재는 키·네트워크가 있어야 한다. 실패해도 기동은 계속한다
    # — 시설 조회만 비고, 접수·요약은 그대로 돌아간다.
    if [ -n "$DATA_GO_KR_KEY" ]; then
        echo "[entrypoint] 공공데이터 적재 시도"
        python -m donghaenggori.services.loader || \
            echo "[entrypoint] 공공데이터 적재 실패 — 시설 조회 없이 계속합니다"
    else
        echo "[entrypoint] DATA_GO_KR_KEY 없음 — 공공데이터 미연동으로 기동"
    fi
else
    echo "[entrypoint] 기존 DB 사용 ($DB)"
fi

exec "$@"
