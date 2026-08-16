"""사용자 생성 / 비밀번호 설정 — 운영자 전용 대화형 CLI.

비밀번호는 인자·환경변수로 받지 않는다(쉘 히스토리·프로세스 목록에 남는다).
getpass 로만 입력받는다. 같은 사용자 ID로 다시 실행하면 비밀번호 재설정으로도 쓴다
(U001/U002처럼 이름만 시드돼 있고 비밀번호가 없는 기본 계정에 처음 비밀번호를
넣을 때도 이 스크립트를 쓴다).

실행:
    python -m donghaenggori.services.create_user
"""
from __future__ import annotations

import getpass

from ..core import db

ROLES = ("사회복지사", "동행매니저", "관리자")


def run() -> None:
    db.init_db()
    user_id = input("사용자 ID (예: U001): ").strip()
    name = input("이름: ").strip()
    role = input(f"역할 {ROLES}: ").strip()
    if role not in ROLES:
        print(f"알 수 없는 역할: {role!r} — {ROLES} 중 하나여야 합니다")
        return
    email = input("이메일: ").strip()
    pw1 = getpass.getpass("비밀번호: ")
    pw2 = getpass.getpass("비밀번호 확인: ")
    if pw1 != pw2:
        print("비밀번호가 일치하지 않습니다.")
        return
    if len(pw1) < 8:
        print("비밀번호는 8자 이상이어야 합니다.")
        return
    try:
        user = db.create_user(user_id, name, role, email, pw1)
    except ValueError as e:
        print(f"실패: {e}")
        return
    print(f"완료: {user['id']} {user['name']} ({user['role']}) <{user['email']}>")


if __name__ == "__main__":
    run()
