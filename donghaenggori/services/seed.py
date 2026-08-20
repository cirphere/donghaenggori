"""시드 데이터 생성 — 케어 프로필 20건 · 과거 동행 이력 60건 (파일1 100% 기준).

전부 가상 데이터다. 실제 개인정보를 포함하지 않는다.

설계 의도
  · 시나리오에 쓰이는 3명(박순자·김수남·정말순)은 고정 — 문서·시연과 어긋나면 안 된다.
  · 이력 패턴을 의도적으로 배분해 이력 기반 상태가 모두 재현되게 한다.
      추정     : 방문 이력 있음(1회든 단골 2회 이상이든 이력만으로는 여기까지다)
      확인 필요 : 이력 없음(신규) 또는 후보 동률
    '확인됨' 은 시드로 만들 수 없다 — 어르신이 이번 발화에서 병원명을 직접
    말했을 때만 붙기 때문이다(core.hospital 설명 참조).
  · 광주·전남을 섞는다 — 광주 대상자가 있어야 복지관(C-DS03) RAG 매칭이 동작한다.
"""
from __future__ import annotations

import datetime
import json
import os
import random
import secrets

from ..core import db

TODAY = datetime.date(2026, 8, 7)
OUT_PATH = os.path.join(db.DATA_DIR, "care_profiles.json")

# 시드 실행 때 함께 생성할 데모 로그인 계정 — **비밀번호는 여기 적지 않는다.**
#
# 배포본이 인터넷에 열려 있어서, 소스에 적은 값은 곧 "주소만 알면 누구나 칠 수
# 있는 값"이 된다. 실제로 여기 박아 둔 관리자 비밀번호로 공개 주소에서 로그인이
# 됐다. 그 토큰이면 어르신 전원의 건강 상태·보호자 전화·독거 여부가 원격으로
# 나온다. 값을 기억하기 쉽게 고르는 것도 같은 문제다 — 아예 두지 않는다.
#
# 비밀번호는 `SEED_PASSWORD_<아이디>` 환경변수로 받는다(.env.app).
SEED_USERS = (
    ("test1", "테스트 사회복지사", "사회복지사"),
    ("test2", "테스트 동행매니저", "동행매니저"),
    ("admin", "관리자", "관리자"),
)

# 시나리오 고정 3명 — 파일2·3·4에서 참조되므로 값을 바꾸지 않는다
FIXED = {
    "010-1234-5678": {
        # **광주 용봉동에 둔다.** 전남 고흥군은 반경 6km 안에 병원이 거의
        # 없어 '주변 병원 후보' 가 빈 채로 나온다 — 농촌의 현실이지만 시연에서
        # 보여줄 것이 없다. 심평원 조회가 실제로 도는 것을 보이려면 병원이
        # 있는 지역이어야 한다.
        "id": "P001", "name": "박순자", "age": 81,
        "region": "광주광역시 북구", "address": "용봉동",
        "guardian": {"name": "이지현", "relation": "딸", "phone": "010-9876-5432",
                     "available": "평일 18시 이후"},
        "caregiver": "김복지 생활지원사", "mobility": "거동 불편(보행기 사용)",
        "fall_risk": True, "lives_alone": True, "preferred_time": "오전",
        "notes": "무릎 통증 지속, 큰 소리로 천천히 안내 필요",
        "history": [
            {"date": "2026-03-05", "hospital": "○○정형외과의원", "dept": "정형외과", "symptom": "무릎 통증", "pharmacy": True},
            {"date": "2026-05-12", "hospital": "○○정형외과의원", "dept": "정형외과", "symptom": "무릎 통증", "pharmacy": True},
            {"date": "2026-06-20", "hospital": "△△내과의원", "dept": "내과", "symptom": "혈압약 처방", "pharmacy": True},
            # 안과 이력 — "눈이 침침해서" 라고만 말해도 지난번 병원이 후보로
            # 나오는 것을 보이는 자리다. 증상 사전이 안과를 잡고, 그 진료과의
            # 이력에서 병원이 따라온다.
            {"date": "2026-07-14", "hospital": "밝은눈안과", "dept": "안과", "symptom": "눈이 침침함", "pharmacy": False},
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
    # **이름이 없는 프로필.** 기관이 명단·주소부터 올리고 성함은 나중에
    # 채우는 경우가 실제로 있다. 통화가 이때 성함을 물어야 하고(voice.
    # _has_real_name), 주소만으로도 날씨·병원 후보가 나와야 한다.
    "010-4444-5555": {
        "id": "P004", "name": "", "age": None,
        "region": "전남 여수시", "address": "여서로",
        "guardian": None, "caregiver": None,
        "mobility": None, "fall_risk": False, "lives_alone": True,
        "preferred_time": None,
        "notes": "성함 미확인 — 확인 전화 필요",
        "history": [],
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
    """프로필 7건 · 이력 20건을 결정적으로 생성한다.

    20건에서 줄였다. 시연에서 목록을 훑을 때 20명은 스크롤만 길고, 어느
    어르신을 봐야 하는지 알 수 없다. 고정 4명(시나리오용)에 생성 3명이면
    이력 있음·없음·신규가 다 나온다.
    """
    rnd = random.Random(seed)
    profiles: dict[str, dict] = {
        "_comment": ("발신번호 → 케어 프로필. 시연용 가상 데이터(실제 개인정보 아님). "
                     "history는 과거 동행 이력. 병원 후보 3단계 재현을 위해 패턴을 배분함."),
    }
    profiles.update({k: json_copy(v) for k, v in FIXED.items()})

    fixed_hist = sum(len(v["history"]) for v in FIXED.values())   # 5건
    target_hist = 20
    n_new = 7 - len(FIXED)                                         # 3명
    used_names = {v["name"] for v in FIXED.values()}

    # 남은 이력을 생성 인원에게 배분 — 0~5건씩.
    #
    # **담을 수 있는 양을 넘겨 달라고 하면 안 된다.** 예전에는 목표 건수를
    # 그대로 while 로 돌렸는데, 인원을 20명에서 7명으로 줄이자 3명(최대
    # 5건씩 = 10건)에게 15건을 나누라는 요구가 되어 루프가 끝나지 않았다.
    # 담을 수 있는 만큼으로 먼저 자른다.
    per = [0] * n_new
    # 1명은 신규(이력 0)로 남겨 cold start 재현 — 그 사람은 배분에서 뺀다.
    idx = list(range(1, n_new))
    cap = len(idx) * 5
    remain = min(target_hist - fixed_hist, cap)
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

        # 단골 병원 1~2곳을 정해두고 이력을 생성 → '추정/확인 필요'가 자연히 갈린다
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


def _seed_password(user_id: str) -> tuple[str, bool] | None:
    """(비밀번호, 난수로_만들었나) 또는 None(=이 계정은 건드리지 않는다).

    `create_user` 는 upsert 라 부를 때마다 비밀번호가 덮인다. 그래서 환경변수가
    없을 때 계정이 **이미 있으면 그냥 둔다** — 재시드가 운영자가 바꿔 둔
    비밀번호를 말없이 갈아엎으면, 시연 직전에 로그인이 막히고 원인도 안 보인다.

    없는 계정만 난수로 만든다. 기동은 되면서 추측할 수 있는 값은 안 남는다.
    """
    env = os.environ.get(f"SEED_PASSWORD_{user_id.upper()}", "").strip()
    if env:
        return env, False
    if db.get_user_by_id(user_id):
        return None
    return secrets.token_urlsafe(9), True


def write_and_load(seed: int = 20260807, verbose: bool = True) -> dict:
    """생성 → care_profiles.json 저장 → DB 재적재."""
    profiles = build(seed)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)

    db.reset_db()      # 시드 파일을 다시 읽어 적재
    # 난수로 만든 것만 출력한다. 환경변수로 받은 값은 이미 운영자가 아는
    # 값이고, 로그에 남길 이유가 없다.
    generated: list[tuple[str, str]] = []
    for user_id, name, role in SEED_USERS:
        got = _seed_password(user_id)
        if got is None:
            continue
        password, was_generated = got
        db.create_user(user_id, name, role, password)
        if was_generated:
            generated.append((user_id, password))

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
    # verbose 와 무관하게 찍는다. DB 에는 해시만 남아 여기서 놓치면 그 계정으로
    # 들어갈 방법이 없다 — 조용히 삼키면 로그인 불가 계정만 만들어 놓는 셈이다.
    if generated:
        print("\n  계정 비밀번호를 새로 만들었습니다 (이 출력에만 나옵니다):")
        for user_id, password in generated:
            print(f"    {user_id:<8} {password}")
        print("  고정하려면 .env.app 에 SEED_PASSWORD_<아이디> 로 넣으세요.")
    return stats


if __name__ == "__main__":
    write_and_load()
