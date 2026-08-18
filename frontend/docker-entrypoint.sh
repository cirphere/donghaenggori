#!/bin/sh
# 프론트 컨테이너 기동 — 기본 인증 설정을 만들고 nginx 를 띄운다.
#
# 비밀번호는 저장소에 넣지 않는다. .env 의 STAFF_USER/STAFF_PASSWORD 를 받아
# 기동할 때마다 해시(bcrypt)를 만들어 컨테이너 안에만 둔다.
set -e

AUTH_CONF=/etc/nginx/auth.conf
HTPASSWD=/etc/nginx/.htpasswd

if [ -n "$STAFF_USER" ] && [ -n "$STAFF_PASSWORD" ]; then
    htpasswd -cbB "$HTPASSWD" "$STAFF_USER" "$STAFF_PASSWORD" >/dev/null 2>&1
    # nginx 워커는 nginx 사용자로 돈다. root:600 으로 두면 워커가 못 읽어
    # 인증이 500 으로 떨어진다 — 로그인해도 안 들어가진다.
    chown root:nginx "$HTPASSWD"
    chmod 640 "$HTPASSWD"
    cat > "$AUTH_CONF" <<EOF
auth_basic "동행고리 AI";
auth_basic_user_file $HTPASSWD;
EOF
    echo "[frontend] 기본 인증 켜짐 (/docs·/dev 전용) — 사용자: $STAFF_USER"
else
    # 자격증명이 없으면 /docs·/dev 가 열린다. **데이터는 아니다** —
    # API 는 로그인 토큰이 지키고, 그건 이 설정과 무관하다.
    #
    # 예전에는 이 경고가 "주소를 아는 누구나 접수 목록을 보고 확정할 수 있다"
    # 였다. 백엔드에 인증이 없던 시절엔 사실이었지만 지금은 아니다. 틀린 경고를
    # 크게 띄우면 진짜 경고를 흘려보게 된다.
    echo "auth_basic off;" > "$AUTH_CONF"
    echo "[frontend] 알림: STAFF_USER/STAFF_PASSWORD 가 없어 /docs·/dev 가 공개됩니다."
    echo "[frontend]       (접수 데이터는 API 토큰이 지키므로 영향 없음)"
fi

exec nginx -g 'daemon off;'
