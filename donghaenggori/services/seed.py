"""시드 데이터 생성 — 케어 프로필 20건 · 과거 동행 이력 60건 (파일1 100% 기준).

전부 가상 데이터다. 실제 개인정보를 포함하지 않는다.

설계 의도
  · 시나리오에 쓰이는 3명(박순자·김수남·정말순)은 고정 — 문서·시연과 어긋나면 안 된다.
  · 병원 후보 3단계가 전부 재현되도록 이력 패턴을 의도적으로 배분한다.
      확인됨   : 최근 6개월 같은 병원 2회 이상
      추정     : 1회 방문
      확인 필요 : 이력 없음(신규) 또는 후보 동률
  · 광주·전남을 섞는다 — 광주 대상자가 있어야 복지관(C-DS03) RAG 매칭이 동작한다.
"""
from __future__ import annotations

import datetime
import json
import os
import random

from ..core import db

TODAY = datetime.date(2026, 8, 7)
OUT_PATH = os.path.join(db.DATA_DIR, "care_profiles.json")

# 시드 실행 때 함께 생성할 데모 로그인 계정.
SEED_USERS = (
    ("test1", "테스트 사회복지사", "사회복지사", "12341234"),
    ("test2", "테스트 동행매니저", "동행매니저", "12341234"),
    ("admin", "관리자", "관리자", "admin1234"),
)

# 시나리오 고정 3명 — 파일2·3·4에서 참조되므로 값을 바꾸지 않는다
FIXED = {
    "010-1234-5678": {
        "id": "P001", "name": "박순자", "age": 81, "region": "전남 고흥군 ○○면",
        "guardian": {"name": "이지현", "relation": "딸", "phone": "010-9876-5432",
                     "available": "평일 18시 이후"},
        "caregiver": "김복지 생활지원사", "mobility": "거동 불편(보행기 사용)",
        "fall_risk": True, "lives_alone": True, "preferred_time": "오전",
        "notes": "무릎 통증 지속, 큰 소리로 천천히 안내 필요",
        "history": [
            {"date": "2026-03-05", "hospital": "○○정형외과의원", "dept": "정형외과", "symptom": "무릎 통증", "pharmacy": True},
            {"date": "2026-05-12", "hospital": "○○정형외과의원", "dept": "정형외과", "symptom": "무릎 통증", "pharmacy": True},
            {"date": "2026-06-20", "hospital": "△△내과의원", "dept": "내과", "symptom": "혈압약 처방", "pharmacy": True},
        ],
    },
    "010-2222-3333": {
        "id": "P002", "name": "김수남", "age": 78, "region": "전남 신안군 ○○면(섬)",
        "guardian": {"name": "김영호", "relation": "아들", "phone": "010-5555-6666", "available": "주말"},
        "caregiver": "이돌봄 생활지원사", "mobility": "보행 가능, 장거리 이동 시 차량 필요",
        "fall_risk": False, "lives_alone": True, "preferred_time": "오후",
        "notes": "섬 지역, 배편 시간 확인 필요",
        "history": [
            {"date": "2026-04-02", "hospital": "□□정형외과", "dept": "정형외과", "symptom": "허리", "pharmacy": False},
            {"date": "2026-05-10", "hospital": "◇◇재활의학과", "dept": "재활의학과", "symptom": "허리 물리치료", "pharmacy": False},
        ],
    },
    "010-7777-8888": {
        "id": "P003", "name": "정말순", "age": 85, "region": "전남 보성군 ○○리",
        "guardian": None, "caregiver": "박돌봄 생활지원사", "mobility": "휠체어 필요",
        "fall_risk": True, "lives_alone": True, "preferred_time": "오전",
        "notes": "보호자 없음 — 동행 매니저 필수",
        "history": [],       # 신규(cold start) → '확인 필요' 재현용
    },
}

_SURNAME = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권"]
_GIVEN = ["순자", "말순", "영자", "복순", "정례", "귀남", "옥자", "삼순", "덕례", "명자",
          "판례", "금순", "춘자", "월자", "화자", "일남", "두석", "종수"]
_GWANGJU = ["광주광역시 동구", "광주광역시 서구", "광주광역시 남구",
            "광주광역시 북구", "광주광역시 광산구"]
_JEONNAM = ["전남 담양군", "전남 곡성군", "전남 화순군", "전남 영광군",
            "전남 강진군", "전남 장성군", "전남 함평군"]
_DEPTS = ["정형외과", "내과", "안과", "이비인후과", "치과", "재활의학과", "피부과", "신경과"]
_SYMPTOM = {"정형외과": "무릎 통증", "내과": "혈압약 처방", "안과": "백내장 경과",
            "이비인후과": "이명", "치과": "잇몸 치료", "재활의학과": "물리치료",
            "피부과": "가려움", "신경과": "어지럼 경과"}
_MARK = ["○○", "△△", "□□", "◇◇", "◎◎", "☆☆"]
_MOBILITY = ["거동 불편(보행기 사용)", "휠체어 필요", "보행 가능, 장거리 이동 시 차량 필요",
             "보행 가능", "지팡이 사용"]


def _hospital(mark: str, dept: str) -> str:
    return f"{mark}{dept}" + ("의원" if dept in ("정형외과", "내과") else "")


def build(seed: int = 20260807) -> dict:
    """프로필 20건 · 이력 60건을 결정적으로 생성한다."""
    rnd = random.Random(seed)
    profiles: dict[str, dict] = {
        "_comment": ("발신번호 → 케어 프로필. 시연용 가상 데이터(실제 개인정보 아님). "
                     "history는 과거 동행 이력. 병원 후보 3단계 재현을 위해 패턴을 배분함."),
    }
    profiles.update({k: json_copy(v) for k, v in FIXED.items()})

    fixed_hist = sum(len(v["history"]) for v in FIXED.values())   # 5건
    target_hist = 60
    n_new = 20 - len(FIXED)                                        # 17명
    used_names = {v["name"] for v in FIXED.values()}

    # 남은 이력을 17명에게 배분 — 0~5건씩, 합계가 target에 맞게
    remain = target_hist - fixed_hist                              # 55건
    per = [0] * n_new
    # 2명은 신규(이력 0)로 남겨 cold start 재현
    idx = list(range(2, n_new))
    while remain > 0:
        i = rnd.choice(idx)
        if per[i] >= 5:
            continue
        per[i] += 1
        remain -= 1

    for i in range(n_new):
        while True:
            name = rnd.choice(_SURNAME) + rnd.choice(_GIVEN)
            if name not in used_names:
                used_names.add(name)
                break
        # 절반은 광주(복지관 RAG 매칭용), 절반은 전남
        region = (rnd.choice(_GWANGJU) if i % 2 == 0 else rnd.choice(_JEONNAM))
        has_guardian = rnd.random() < 0.6
        phone = f"010-{rnd.randint(2000,9989):04d}-{rnd.randint(1000,9999):04d}"

        # 단골 병원 1~2곳을 정해두고 이력을 생성 → '확인됨/추정'이 자연히 갈린다
        pool_depts = rnd.sample(_DEPTS, k=rnd.randint(1, 2))
        pool = [(d, _hospital(rnd.choice(_MARK), d)) for d in pool_depts]
        hist = []
        for _ in range(per[i]):
            dept, hosp = rnd.choice(pool)
            d = TODAY - datetime.timedelta(days=rnd.randint(10, 200))
            hist.append({"date": d.isoformat(), "hospital": hosp, "dept": dept,
                         "symptom": _SYMPTOM[dept], "pharmacy": rnd.random() < 0.6})
        hist.sort(key=lambda h: h["date"])

        mobility = rnd.choice(_MOBILITY)
        profiles[phone] = {
            "id": f"P{i+4:03d}", "name": name, "age": rnd.randint(72, 91),
            "region": f"{region} ○○동" if "광주" in region else f"{region} ○○면",
            "guardian": ({"name": rnd.choice(_SURNAME) + rnd.choice(["영호", "지현", "민준", "서연"]),
                          "relation": rnd.choice(["아들", "딸"]),
                          "phone": f"010-{rnd.randint(2000,9989):04d}-{rnd.randint(1000,9999):04d}",
                          "available": rnd.choice(["평일 18시 이후", "주말", "상시"])}
                         if has_guardian else None),
            "caregiver": rnd.choice(["김복지", "이돌봄", "박돌봄", "최돌봄", "정복지"]) + " 생활지원사",
            "mobility": mobility,
            "fall_risk": "휠체어" in mobility or rnd.random() < 0.4,
            "lives_alone": rnd.random() < 0.7,
            "preferred_time": rnd.choice(["오전", "오후"]),
            "notes": rnd.choice(["큰 소리로 천천히 안내 필요", "계단 이동 곤란",
                                 "청력 약함", "당뇨 관리 중", ""]) or None,
            "history": hist,
        }
    return profiles


def json_copy(v: dict) -> dict:
    return json.loads(json.dumps(v, ensure_ascii=False))


def write_and_load(seed: int = 20260807, verbose: bool = True) -> dict:
    """생성 → care_profiles.json 저장 → DB 재적재."""
    profiles = build(seed)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)

    db.reset_db()      # 시드 파일을 다시 읽어 적재
    for user_id, name, role, password in SEED_USERS:
        db.create_user(user_id, name, role, password)

    conn = db.get_conn()
    n_p = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    n_h = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    regions = conn.execute(
        "SELECT CASE WHEN region LIKE '광주%' THEN '광주' ELSE '전남' END r, COUNT(*) c "
        "FROM profiles GROUP BY r").fetchall()
    conn.close()

    stats = {"profiles": n_p, "history": n_h, "regions": {r["r"]: r["c"] for r in regions}}
    if verbose:
        print(f"  프로필 {n_p}건 / 이력 {n_h}건")
        print(f"  지역 분포: {stats['regions']}")
    return stats


if __name__ == "__main__":
    write_and_load()
