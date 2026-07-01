"""케어 프로필 조회 — 발신번호 기반 (계획서 기능 1).

전화번호는 1차 식별 단서일 뿐, 최종 확정은 사회복지사가 한다(안전장치).
미등록 번호 → 신규(cold start)로 처리.
데이터는 SQLite(db.py)에서 읽는다. 최초 실행 시 care_profiles.json이 자동 시드된다.
"""
from __future__ import annotations

from . import db


def normalize(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11:  # 01012345678 → 010-1234-5678
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return phone.strip()


# 하위 호환 별칭
_normalize = normalize


def lookup(phone: str) -> dict | None:
    """발신번호로 케어 프로필을 찾는다(과거 이력 포함). 없으면 None(신규)."""
    return db.get_profile(normalize(phone))
