"""SQLite 데이터 계층 — 표준 sqlite3, 파일 하나(donghaenggori.db).

테이블 (문서 매핑)
  profiles      대상자 케어 프로필
  history       과거 동행 이력 — 병원 후보 추정의 1차 근거
  intakes       접수 기록 + 사회복지사 확정 결과
  post_records  동행 후 사후기록 초안·승인 (화면 05)
  audit_log     확정·수정 이력 (화면 01·03·05 "모든 변경은 감사 로그에 남는다")
  users         RBAC 역할 분리 (화면 01 "권한: 접수 확정·수정")
  facilities    지역 복지자원 — 공공데이터 CSV 적재, RAG 검색 대상

민감 건강정보가 포함되므로 실운영에선 비식별·암호화 필요(시연은 가상 데이터).
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading

_ROOT = os.path.dirname(os.path.dirname(__file__))          # donghaenggori/
DATA_DIR = os.path.join(_ROOT, "data")
# DB 위치는 환경변수로 뺄 수 있다. 컨테이너에서 데이터를 볼륨에 두기 위해서다
# (SQLite는 -wal/-shm 형제 파일을 만들어, 파일 하나만 바인드마운트하면 깨진다).
DB_PATH = os.environ.get("DONGHAENGGORI_DB") or os.path.join(DATA_DIR, "donghaenggori.db")
_SEED_PROFILES = os.path.join(DATA_DIR, "care_profiles.json")

_lock = threading.Lock()
_inited = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  phone TEXT PRIMARY KEY,
  id TEXT, name TEXT, age INTEGER, region TEXT,
  guardian_json TEXT,
  caregiver TEXT, mobility TEXT,
  fall_risk INTEGER, lives_alone INTEGER,
  preferred_time TEXT, notes TEXT,
  ltci_grade TEXT,        -- 장기요양등급 1~5 · 인지지원 (공단 판정)
  care_program TEXT,      -- 노인맞춤돌봄서비스 군 (지자체 선정)
  address TEXT            -- 상세 주소. region 은 읍면동까지, 여기는 그 뒤
);

CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT NOT NULL,
  date TEXT, hospital TEXT, dept TEXT, symptom TEXT,
  pharmacy INTEGER DEFAULT 0,
  source TEXT DEFAULT 'seed'
);
CREATE INDEX IF NOT EXISTS idx_history_phone ON history(phone);

CREATE TABLE IF NOT EXISTS intakes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT,
  channel TEXT DEFAULT '전화',            -- 전화 | 앱·웹(보호자) | 직접(기관)
  phone TEXT, target TEXT,
  raw_utterance TEXT, intent TEXT,
  hospital TEXT, hospital_status TEXT, dept TEXT,
  date_value TEXT, date_label TEXT,
  time_value TEXT,
  access_code TEXT,      -- 보호자 조회용 신청번호(무인증 조회 열쇠)
  need_level TEXT,
  status TEXT DEFAULT '접수 대기',        -- 접수 대기 | 확정 | 임시 접수 | 긴급
  confirmed INTEGER DEFAULT 0,
  confirmed_hospital TEXT, confirmed_date TEXT, confirmed_level TEXT,
  identity_answer TEXT,   -- 전화에서 '맞으실까요' 에 답한 내용 (원문 그대로)
  identity_status TEXT,   -- 확인됨 | 추정 | 확인 필요 — 확정은 사람이 한다
  card_json TEXT,         -- 접수 당시 생성된 카드 전문(근거·확인질문 포함)
  transfer_status TEXT,   -- 긴급 전환 결과 (연결됨 | 통화중 | 응답없음 | 실패)
  manager TEXT            -- 동행 담당자(동행매니저 이름). 없으면 '배정 필요'
);

CREATE TABLE IF NOT EXISTS post_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intake_id INTEGER, phone TEXT,
  created_at TEXT,
  memo_raw TEXT,                          -- 매니저 음성 메모 원문
  treatment TEXT,                         -- 진료 내용
  next_visit TEXT,                        -- 다음 진료
  pharmacy TEXT,                          -- 약국 방문
  cautions TEXT,                          -- 다음 동행 주의사항
  guardian_msg TEXT,                      -- 보호자 공유 메시지 초안
  profile_update TEXT,                    -- 케어 프로필 업데이트 제안
  approved INTEGER DEFAULT 0,             -- 사회복지사 승인 여부
  draft_json TEXT,                        -- AI가 처음 낸 초안(수정 전) 스냅샷
  outcome TEXT,                           -- 진료 정상 완료 | 일부만 진행 | 진료 못 함
  depart_at TEXT, return_at TEXT,         -- 출발·복귀 시각 (HH:MM)
  saved INTEGER DEFAULT 0,                -- 임시 저장 여부(승인과 별개)
  reviewed INTEGER DEFAULT 0              -- 사람이 판단을 내렸나(승인이든 거절이든)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, actor TEXT, role TEXT,
  action TEXT,                            -- 확정 | 수정 | 승인 | 거절 | 이력추가
  target_type TEXT, target_id TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT, role TEXT   -- 사회복지사 | 동행매니저 | 관리자
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,    -- sha256(원문 토큰) — 원문은 저장하지 않는다
  user_id TEXT NOT NULL,
  created_at TEXT, expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS facilities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT,                            -- C-DS03 | C-DS04 | C-DS06 | C-DS17 ...
  name TEXT, kind TEXT, region TEXT,
  address TEXT, phone TEXT,
  lat REAL, lon REAL
);
CREATE INDEX IF NOT EXISTS idx_fac_region ON facilities(region);
"""

# 역할별 권한 — 화면 01의 "권한: 접수 확정·수정 (RBAC)"
ROLE_PERMISSIONS = {
    "사회복지사": {"intake.view", "intake.confirm", "intake.edit", "post.approve", "audit.view"},
    "동행매니저": {"intake.view", "post.write"},
    "관리자": {"intake.view", "intake.confirm", "intake.edit", "post.approve", "audit.view", "admin"},
}


def can(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())


def _with_permissions(user: dict | None) -> dict | None:
    """사용자 dict 에 권한 목록을 붙인다 — 화면이 버튼을 가릴 근거.

    화면에서 역할 이름을 하드코딩하지 않게 하려는 것이다. `role === "사회복지사"`
    같은 판정을 JS 에 두면 권한표가 두 곳이 되고, 역할이 하나 늘 때 한쪽만
    고치면 화면과 서버가 어긋난다. 서버가 목록을 주면 화면은 포함 여부만 본다.

    화면이 가리는 것은 안내일 뿐이고 **실제 경계는 서버의 403** 이다.
    """
    if not user:
        return user
    user = dict(user)
    user["permissions"] = sorted(ROLE_PERMISSIONS.get(user.get("role"), set()))
    return user


# ------------------------------------------------------------------ 인증 --
# 새 pip 의존성을 안 늘리려고 표준 라이브러리만 쓴다. bcrypt 급 보안이 필요한
# 규모가 아니라(내부 소수 인원용) PBKDF2-HMAC-SHA256으로 충분하다.

_PBKDF2_ITERATIONS = 260_000


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${_PBKDF2_ITERATIONS}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, iters_s, dk_hex = stored.split("$")
        salt, iters, expected = bytes.fromhex(salt_hex), int(iters_s), bytes.fromhex(dk_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user(user_id: str, name: str, role: str, password: str) -> dict:
    """운영자 부트스트랩 전용. 같은 user_id로 다시 부르면 비밀번호 재설정도 겸한다.

    로그인은 **아이디**로 한다 — 기관 계정은 직원번호(U001)로 부르는 게
    자연스럽고, 시연장에서 아이디 네 글자가 가장 빠르다.

    아이디는 대소문자를 가리지 않고 조회하므로(get_user_by_id), 여기서도
    'u001' 과 'U001' 이 다른 계정이 되지 않게 막는다.
    """
    init_db()
    user_id = (user_id or "").strip()
    if not user_id:
        raise ValueError("아이디가 비었습니다")
    conn = get_conn()
    try:
        dup = conn.execute("SELECT id FROM users WHERE id=? COLLATE NOCASE AND id<>?",
                           (user_id, user_id)).fetchone()
        if dup:
            raise ValueError(f"대소문자만 다른 아이디가 이미 있습니다: {dup['id']}")
        pw_hash = _hash_password(password)
        conn.execute(
            "INSERT INTO users (id,name,role,password_hash) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role, "
            "password_hash=excluded.password_hash",
            (user_id, name, role, pw_hash))
        # 비밀번호를 바꾸면 그 사람의 기존 세션도 끊는다.
        #
        # 안 끊으면 토큰이 유출됐을 때 비밀번호를 재설정해도 소용이 없다 —
        # 훔친 토큰이 만료(기본 12시간)까지 그대로 살아서 확정도 하고 감사
        # 로그도 읽는다. 운영자는 "막았다" 고 믿는데 안 막힌 상태가 된다.
        # 토큰을 폐기할 경로가 이것 말고는 없다.
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
        return {"id": user_id, "name": name, "role": role}
    finally:
        conn.close()


def get_user_by_id(user_id: str) -> dict | None:
    """아이디로 사용자 조회. **대소문자를 가리지 않는다.**

    아이디가 U001 같은 직원번호라 소문자로 치는 일이 잦다. 시연 중에 그걸로
    막히면 손해가 크고, 대소문자를 구분해서 얻는 것도 없다.
    """
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=? COLLATE NOCASE",
                           ((user_id or "").strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def verify_login(user_id: str, password: str) -> dict | None:
    """아이디+비밀번호 검증. 성공하면 password_hash 를 뺀 사용자 dict, 실패하면 None."""
    user = get_user_by_id(user_id)
    if not user or not user.get("password_hash"):
        return None
    if not _verify_password(password, user["password_hash"]):
        return None
    user = dict(user)
    user.pop("password_hash", None)
    return _with_permissions(user)


def create_session(user_id: str, ttl_seconds: int) -> str:
    """세션을 만들고 원문 토큰을 반환한다 — 이번 호출에서만 노출, DB엔 해시만 남는다."""
    init_db()
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.now() + datetime.timedelta(seconds=ttl_seconds)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
            (_hash_token(token), user_id, _now(), _ts(expires)))
        conn.commit()
    finally:
        conn.close()
    return token


def resolve_session(token: str) -> dict | None:
    """원문 토큰 → 유효하면 사용자 dict(password_hash 제외), 없거나 만료면 None."""
    init_db()
    conn = get_conn()
    try:
        token_hash = _hash_token(token)
        row = conn.execute(
            "SELECT s.expires_at, u.id, u.name, u.role FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (token_hash,)).fetchone()
        if not row:
            return None
        # 같은 형식으로 만든 문자열끼리 비교한다. 예전엔 만료를 초까지
        # ("%H:%M:%S"), 현재 시각을 분까지("%H:%M") 찍어 놓고 문자열로
        # 비교했다 — 접두사가 같고 길이가 달라서 만료가 최대 59초 늦게
        # 걸렸고, 맞아떨어진 것도 길이 덕분이지 의도가 아니었다. 한쪽
        # 포맷만 바꾸면 조용히 뒤집힌다.
        if row["expires_at"] < _ts():
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            conn.commit()
            return None
        return _with_permissions(
            {"id": row["id"], "name": row["name"], "role": row["role"]})
    finally:
        conn.close()


def delete_session(token: str) -> None:
    init_db()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ 연결 --

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(force: bool = False) -> None:
    global _inited
    with _lock:
        if _inited and not force:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)  # DB를 볼륨으로 뺀 경우
        conn = get_conn()
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
                _seed_profiles(conn)
            conn.commit()
            _inited = True
        finally:
            conn.close()


# 나중에 늘어난 컬럼들. CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블을
# 건드리지 않으므로, 배포된 DB(데스크탑·컨테이너 볼륨)에는 직접 붙여야 한다.
# 데이터를 지우지 않고 올리기 위한 최소한의 마이그레이션이다.
_ADDED_COLUMNS = [
    ("profiles", "ltci_grade", "TEXT"),
    ("profiles", "care_program", "TEXT"),
    ("intakes", "identity_answer", "TEXT"),
    ("intakes", "identity_status", "TEXT"),
    ("intakes", "card_json", "TEXT"),
    ("intakes", "transfer_status", "TEXT"),
    ("users", "password_hash", "TEXT"),
    # AI가 처음 낸 초안. 승인 시점의 값과 비교해 사회복지사가 무엇을 고쳤는지
    # 센다(파일1 4-2 '사후기록 초안 수정률'). 원본을 안 남기면 고쳤는지조차
    # 알 수 없어, 사람이 와도 지표가 안 나온다.
    ("post_records", "draft_json", "TEXT"),
    # 방문 시각. date_value 는 컬럼이 있는데 시각만 카드 안에 갇혀 있었다.
    # 그래서 목록 화면이 시각을 못 읽었고(일정 화면이 전부 '시간 미정'),
    # verify("time") 이 행에는 반영되지 않았다 — 상세는 오후 3시인데 목록은
    # 여전히 비어 있는 상태가 된다.
    ("intakes", "time_value", "TEXT"),
    # 보호자가 자기 신청을 조회할 때 쓰는 열쇠. 로그인 없이 여는 문이라
    # 추측 가능하면 안 된다 — 목업의 DH-260817-920(날짜+3자리)은 하루치가
    # 1000 개뿐이라 번호만 돌리면 그날 신청이 전부 열린다.
    ("intakes", "access_code", "TEXT"),
    # 동행 담당자. 없으면 일정이 "배정 필요" 로 남는다 — 배정하지 않은 것과
    # 배정할 사람이 없는 것을 화면이 구분할 수 있어야 한다.
    ("intakes", "manager", "TEXT"),
    ("post_records", "outcome", "TEXT"),
    ("post_records", "depart_at", "TEXT"),
    ("post_records", "return_at", "TEXT"),
    ("post_records", "saved", "INTEGER DEFAULT 0"),
    # approved 0 이 "아직 안 봤다" 와 "보고 반영하지 않기로 했다" 를 같이
    # 뜻해서, 거절이 아무 흔적도 남기지 못했다. 판단을 내렸다는 사실을
    # 따로 둔다.
    ("post_records", "reviewed", "INTEGER DEFAULT 0"),
    ("profiles", "address", "TEXT"),
    # 보호자 포털(Next)이 보낸 구조화 신청 원문. AI가 발화에서 뽑은 값과
    # 달리 보호자가 실제로 고른 값 그대로라, guardian_lookup 이 확정 전에도
    # 정직하게 돌려줄 수 있다(§ web/api.py guardian_lookup).
    ("intakes", "guardian_form_json", "TEXT"),
]

# 반대로 **없애는** 컬럼. 이미 만들어진 DB(데스크탑·배포본)에서도 지워야 해서
# 목록으로 둔다. 없으면 조용히 넘어간다.
#
# users.email 은 로그인 키였다가 아이디 로그인으로 바뀌면서 쓰이지 않게 됐다.
# 안 쓰는 컬럼을 남겨두면 다음 사람이 "여기 이메일이 있으니 로그인에 쓰겠지"
# 하고 되살릴 여지가 생긴다. 연락처가 필요해지면 그때 목적에 맞는 컬럼을
# 새로 만드는 편이 낫다.
_DROPPED_COLUMNS = [
    ("users", "email"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # DROP COLUMN 은 SQLite 3.35+ 다. 컨테이너(3.46)·개발기 모두 넘는다.
    # 혹시 낮은 환경이면 지우지 못할 뿐 동작에는 지장이 없으므로 삼킨다.
    for table, column in _DROPPED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            except sqlite3.OperationalError:
                pass


def _seed_profiles(conn: sqlite3.Connection) -> None:
    if not os.path.exists(_SEED_PROFILES):
        return
    with open(_SEED_PROFILES, encoding="utf-8") as f:
        data = json.load(f)
    for phone, p in data.items():
        if phone.startswith("_"):
            continue
        upsert_profile(conn, phone, p)
        for h in p.get("history", []):
            conn.execute(
                """INSERT INTO history (phone,date,hospital,dept,symptom,pharmacy,source)
                   VALUES (?,?,?,?,?,?,'seed')""",
                (phone, h.get("date"), h.get("hospital"), h.get("dept"),
                 h.get("symptom"), int(bool(h.get("pharmacy")))))


def upsert_profile(conn: sqlite3.Connection, phone: str, p: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO profiles
           (phone,id,name,age,region,guardian_json,caregiver,mobility,
            fall_risk,lives_alone,preferred_time,notes,ltci_grade,care_program,
            address)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (phone, p.get("id"), p.get("name"), p.get("age"), p.get("region"),
         json.dumps(p.get("guardian"), ensure_ascii=False) if p.get("guardian") else None,
         p.get("caregiver"), p.get("mobility"),
         int(bool(p.get("fall_risk"))), int(bool(p.get("lives_alone"))),
         p.get("preferred_time"), p.get("notes"),
         p.get("ltci_grade"), p.get("care_program"), p.get("address")))


# ------------------------------------------------------------- 프로필/이력 --

def normalize_phone(phone: str) -> str:
    """번호 표기를 하나로 맞춘다. 010-1234-5678 형태가 기준이다.

    통신망은 국가번호를 붙여 준다 — 실제 수신 통화의 발신번호는 보통
    +821012345678 이다. 이걸 그대로 조회하면 등록된 대상자를 못 찾아
    모든 통화가 '신규 대상자(미등록 번호)'가 된다. 전화 연동을 붙이는
    순간 케어 프로필 조회가 통째로 깨지는 자리다.
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    # +82 10 xxxx xxxx → 010 xxxx xxxx
    if digits.startswith("82") and len(digits) >= 11:
        digits = "0" + digits[2:]
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10 and digits.startswith("0"):
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return phone.strip()


def get_profile(phone: str) -> dict | None:
    """발신번호로 케어 프로필 조회(과거 이력 포함). 없으면 None(신규)."""
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE phone=?", (normalize_phone(phone),)).fetchone()
        if row is None:
            return None
        hist = conn.execute(
            "SELECT date,hospital,dept,symptom,pharmacy FROM history WHERE phone=? ORDER BY date",
            (row["phone"],)).fetchall()
        return {
            "phone": row["phone"], "id": row["id"], "name": row["name"], "age": row["age"],
            # region 은 읍면동까지, address 는 그 뒤까지. 목록(list_profiles)에는
            # region 만 싣는다 — 상세 주소는 상세를 열었을 때만 나간다.
            "region": row["region"], "address": row["address"],
            "guardian": json.loads(row["guardian_json"]) if row["guardian_json"] else None,
            "caregiver": row["caregiver"], "mobility": row["mobility"],
            "fall_risk": bool(row["fall_risk"]), "lives_alone": bool(row["lives_alone"]),
            "preferred_time": row["preferred_time"], "notes": row["notes"],
            "ltci_grade": row["ltci_grade"], "care_program": row["care_program"],
            "history": [dict(h) | {"pharmacy": bool(h["pharmacy"])} for h in hist],
        }
    finally:
        conn.close()


def list_profiles(query: str | None = None, limit: int = 50) -> list[dict]:
    """대상자 목록·검색 — 이름 또는 전화번호.

    **목록에는 건강·보호자 정보를 싣지 않는다.** 화면에서 필요한 것은 누구인지
    고르는 데 쓸 최소한이고, 상세는 get_profile 로 따로 받는다. 목록 한 번에
    스무 명의 독거 여부와 보호자 연락처가 통째로 나가면, 그 화면을 여는 것
    자체가 개인정보 열람이 된다.

    전화번호로 찾을 때는 정규화해서 비교한다 — 화면에서 '010-1234-5678' 로
    쳐도 '01012345678' 로 저장된 것을 찾아야 한다.
    """
    init_db()
    conn = get_conn()
    try:
        sql = ("SELECT phone, id, name, age, region, "
               "       (SELECT COUNT(*) FROM history h WHERE h.phone = p.phone) AS visits, "
               "       (SELECT MAX(date) FROM history h WHERE h.phone = p.phone) AS last_visit "
               "FROM profiles p")
        args: list = []
        if query:
            q = (query or "").strip()
            sql += " WHERE name LIKE ? OR phone LIKE ?"
            args += [f"%{q}%", f"%{normalize_phone(q)}%"]
        sql += " ORDER BY name LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def find_by_guardian_phone(phone: str) -> list[dict]:
    """보호자 번호로 대상자 후보를 역조회한다 (대리 접수 대응).

    보호자가 자기 폰으로 전화하면 발신번호가 대상자와 일치하지 않는다.
    이때 보호자 연락처로 등록된 대상자를 후보로 제시하되, 확정은 사람이 한다.

    JSON 문자열을 LIKE 로 훑지 않는다. 발신번호가 그대로 패턴에 들어가면
    "%" 한 글자로 전체 프로필이 후보로 뜬다 — 실제로 phone="%" 를 넣으면
    12명의 이름·거주지가 접수카드에 그대로 실렸다. LIKE '%...%' 는 어차피
    인덱스를 못 타서 전수 스캔이므로, 파이썬에서 정확히 비교하는 편이
    더 안전하면서 비용도 같다.
    """
    init_db()
    target = normalize_phone(phone)
    if not target:
        return []
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT phone, name, region, guardian_json FROM profiles "
            "WHERE guardian_json IS NOT NULL").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            g = json.loads(r["guardian_json"]) or {}
        except (TypeError, ValueError):
            continue
        if normalize_phone(str(g.get("phone") or "")) != target:
            continue
        out.append({"phone": r["phone"], "name": r["name"], "region": r["region"],
                    "guardian_name": g.get("name"), "guardian_relation": g.get("relation")})
    return out


def add_history(phone, date, hospital, dept, symptom=None, pharmacy=False, source="사후메모") -> None:
    """동행 완료 → 이력 누적. 다음 접수의 병원 후보가 더 정확해진다(플라이휠)."""
    init_db()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO history (phone,date,hospital,dept,symptom,pharmacy,source) VALUES (?,?,?,?,?,?,?)",
            (normalize_phone(phone), date, hospital, dept, symptom, int(bool(pharmacy)), source))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ 접수 --

def _card_json(card) -> str | None:
    """카드를 JSON 으로. 긴급 경로는 카드가 없어(_Stub) None 을 남긴다."""
    to_dict = getattr(card, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        return json.dumps(to_dict(), ensure_ascii=False)
    except Exception:
        return None


def save_intake(card, phone: str, channel: str = "전화", status: str = "접수 대기") -> int:
    """접수를 저장한다. **카드 전문을 함께 남긴다.**

    예전에는 평면 필드(병원·날짜·상태)만 저장해서, 목록에서 접수를 열어도
    왜 '확인 필요'인지 알 수 없었다. 근거·확인 질문·항목별 상태가 만든 즉시
    사라졌기 때문이다. 나중에 발화로 다시 돌려 만들 수도 있지만, 그건 그때의
    데이터로 만든 다른 카드다 — 접수 당시 AI가 무엇을 근거로 무엇을 제시했는지
    그대로 남겨야 사람이 판단하고 감사할 수 있다.
    """
    init_db()
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO intakes
               (created_at,channel,phone,target,raw_utterance,intent,hospital,hospital_status,
                dept,date_value,date_label,time_value,need_level,status,card_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), channel, normalize_phone(phone), card.target, card.raw_utterance, card.intent,
             card.hospital, card.hospital_status, card.dept, card.date_value, card.date_label,
             getattr(card, "time_value", None),
             card.need_level, status, _card_json(card)))
        conn.commit()
        iid = cur.lastrowid
        return iid
    finally:
        conn.close()


def confirm_intake(intake_id: int, hospital: str, date: str, level: str,
                   actor: str = "김○○ 사회복지사", role: str = "사회복지사") -> dict:
    """사회복지사 확정. **같은 값으로 다시 불러도 로그가 늘지 않는다.**

    예전에는 부를 때마다 감사 로그를 남겨서, 버튼을 두 번 누르거나 타임아웃
    뒤 재요청이 들어오면 같은 확정이 두 줄로 쌓였다(실측 2건 → 4건). 감사
    로그는 "무슨 일이 있었나"를 답하는 기록인데, 없던 일이 두 번 있었던 것처럼
    보이면 그 기록을 믿을 수 없게 된다.

    값이 실제로 달라졌을 때만 남긴다 — 확정 내용을 고친 것은 사건이 맞다.
    """
    init_db()
    conn = get_conn()
    try:
        before = conn.execute(
            "SELECT confirmed, confirmed_hospital, confirmed_date, confirmed_level"
            " FROM intakes WHERE id=?", (intake_id,)).fetchone()
        if before is None:
            return {"changed": False, "reason": "없는 접수"}

        same = (before["confirmed"] == 1
                and (before["confirmed_hospital"] or "") == (hospital or "")
                and (before["confirmed_date"] or "") == (date or "")
                and (before["confirmed_level"] or "") == (level or ""))
        conn.execute(
            """UPDATE intakes SET confirmed=1, status='확정',
               confirmed_hospital=?, confirmed_date=?, confirmed_level=? WHERE id=?""",
            (hospital, date, level, intake_id))
        conn.commit()
    finally:
        conn.close()

    if same:
        return {"changed": False, "reason": "이미 같은 값으로 확정됨"}
    log_audit(actor, role, "확정" if not before["confirmed"] else "확정 수정",
              "intake", str(intake_id),
              f"병원={hospital} / 방문일={date} / 지원수준={level}")
    return {"changed": True}


# 확인 입력이 건드리는 곳 — 카드 안의 평면 키와 intakes 컬럼.
#
# 카드 JSON 은 항목별 뷰(fields)와 평면 키(hospital, date_value…)를 둘 다 들고
# 있다. 목록 화면은 컬럼을, 카드 화면은 fields 를 읽으므로 셋을 함께 고쳐야
# 한다. 하나만 고치면 목록과 상세가 다른 값을 보여준다.
_VERIFY_TARGETS = {
    "target":   ("target", "target"),
    "hospital": ("hospital", "hospital"),
    "dept":     ("dept", "dept"),
    "date":     ("date_value", "date_value"),
    "time":     ("time_value", "time_value"),
}


def verify_card_field(intake_id: int, field: str, value: str,
                      actor: str = "김○○ 사회복지사",
                      role: str = "사회복지사") -> dict | None:
    """사회복지사가 통화로 확인한 결과를 카드에 반영한다.

    AI 가 만든 값을 **사람이 덮어쓰는** 유일한 경로다. 그래서 근거에 누가
    확인했는지를 남긴다 — 나중에 카드를 보는 사람이 이 값이 추론인지 통화로
    확인한 것인지 구분할 수 있어야 한다.

    대상자를 확인하면 통화에서 받아 적은 성함·주소 칸은 지운다. 그 둘은
    대상자를 알아내려던 단서였고, 대상자가 정해지면 역할이 끝난다. 남겨 두면
    "말한 성함: 김말자 (확인 필요)" 가 확정된 카드에 계속 붙어 있게 된다.
    """
    if field not in _VERIFY_TARGETS:
        return None
    row = get_intake(intake_id)
    if not row:
        return None
    card = row.get("card")
    if not card:
        return None

    flat_key, column = _VERIFY_TARGETS[field]
    card[flat_key] = value
    fields = card.setdefault("fields", {})
    view = fields.setdefault(field, {"label": field})
    view["value"] = value
    view["status"] = "확인됨"
    view["evidence"] = list(view.get("evidence") or []) + [f"통화로 확인함 — {actor}"]
    # 누가 확인했는지를 **구조화된 키**로도 남긴다. 화면이 "전화번호가 일치해서
    # 확인됨"과 "사람이 통화로 확인해서 확인됨"을 갈라야 하는데(발신번호로는
    # 대상자를 확정하지 않는다 — 불변조건 3), 근거 문장을 문자열로 뒤지게 하면
    # 문구를 다듬는 순간 조용히 깨진다.
    view["verified_by"] = actor
    if field == "hospital":
        # 평면 상태 키를 따로 들고 있어서 같이 올려야 한다
        card["hospital_status"] = "확인됨"
    if field == "target":
        for k in ("spoken_name", "spoken_region"):
            fields.pop(k, None)
            card[k] = None

    init_db()
    conn = get_conn()
    try:
        sets, args = ["card_json=?"], [json.dumps(card, ensure_ascii=False)]
        if column:
            sets.append(f"{column}=?")
            args.append(value)
        if field == "hospital":
            sets.append("hospital_status=?")
            args.append("확인됨")
        args.append(intake_id)
        conn.execute(f"UPDATE intakes SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
    finally:
        conn.close()
    log_audit(actor, role, "항목확인", "intake", str(intake_id), f"{field}={value}")
    return get_intake(intake_id)


def _with_card(row: dict) -> dict:
    """card_json 을 파싱해 card 로 붙인다. 옛 접수는 None 이다."""
    raw = row.pop("card_json", None)
    try:
        row["card"] = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        row["card"] = None
    return row


# ------------------------------------------------- 보호자 조회 (무인증) --
#
# 보호자가 로그인 없이 자기 신청 하나만 열어 보는 경로. **여기서 두 가지를
# 동시에 지켜야 한다** — 보호자는 계정을 만들지 않고(어르신 가족에게 계정을
# 요구하면 아무도 안 쓴다), 그렇다고 아무나 남의 신청을 봐서도 안 된다.
#
# 그래서 **신청번호 + 보호자 연락처** 둘을 함께 요구한다.
#   · 신청번호만: 번호가 새면(문자 전달·스크린샷) 그걸로 끝이다
#   · 연락처만: 번호를 아는 사람은 많다
#   · 둘 다: 신청한 본인이 아니면 맞추기 어렵다
#
# 코드는 전화로 불러 줄 수 있어야 해서 **헷갈리는 글자를 뺀다**(0/O, 1/I/L).
# 8자리 × 30글자 = 6560억 가지. 시도 제한과 함께면 대입은 불가능하다.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def new_access_code() -> str:
    """추측할 수 없는 신청번호. DH-YYMMDD-XXXXXXXX 형태."""
    today = datetime.date.today().strftime("%y%m%d")
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"DH-{today}-{body}"


def set_access_code(intake_id: int, code: str) -> None:
    init_db()
    conn = get_conn()
    try:
        conn.execute("UPDATE intakes SET access_code=? WHERE id=?", (code, intake_id))
        conn.commit()
    finally:
        conn.close()


def set_guardian_form(intake_id: int, form_json: str) -> None:
    """보호자가 보낸 구조화 신청 원문을 남긴다. access_code 발급 직후 한 번 호출한다."""
    init_db()
    conn = get_conn()
    try:
        conn.execute("UPDATE intakes SET guardian_form_json=? WHERE id=?", (form_json, intake_id))
        conn.commit()
    finally:
        conn.close()


def find_by_access_code(code: str, phone: str) -> dict | None:
    """신청번호와 보호자 연락처가 **둘 다** 맞아야 돌려준다.

    코드 비교에 compare_digest 를 쓴다. 문자열 == 는 앞에서부터 비교하다
    다른 글자가 나오면 바로 끝나서, 응답 시간으로 몇 글자까지 맞았는지가
    새어 나간다. 여기는 로그인 없이 열려 있어 시도 횟수를 많이 줄 수밖에
    없는 경로라 더 조심한다.
    """
    if not code or not phone:
        return None
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM intakes WHERE phone=? AND access_code IS NOT NULL",
            (normalize_phone(phone),)).fetchall()
    finally:
        conn.close()
    want = (code or "").strip().upper()
    for r in rows:
        if hmac.compare_digest((r["access_code"] or "").upper(), want):
            return dict(r)
    return None


def get_intake(intake_id: int) -> dict | None:
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM intakes WHERE id=?", (intake_id,)).fetchone()
        return _with_card(dict(row)) if row else None
    finally:
        conn.close()


def attach_identity_answer(intake_id: int, answer: str, status: str) -> None:
    """전화 2턴에서 받은 본인 확인 답변을 접수에 붙인다.

    답변을 해석해 사람을 확정하지 않는다. 들은 말을 그대로 남기고 상태만
    조정해서, 사회복지사가 원문을 보고 판단하게 한다.
    """
    init_db()
    conn = get_conn()
    try:
        conn.execute("UPDATE intakes SET identity_answer=?, identity_status=? WHERE id=?",
                     (answer, status, intake_id))
        conn.commit()
    finally:
        conn.close()


def set_transfer_status(intake_id: int, status: str, actor: str = "전화 시스템") -> None:
    """긴급 전환 결과를 접수에 남긴다.

    담당자가 못 받은 것을 아무도 모르는 상태가 제일 위험하다. 어르신은
    "연결이 어렵습니다" 안내를 듣고 끊지만, 그 사실이 어디에도 없으면
    아무도 다시 걸지 않는다.
    """
    init_db()
    conn = get_conn()
    try:
        conn.execute("UPDATE intakes SET transfer_status=? WHERE id=?", (status, intake_id))
        conn.commit()
    finally:
        conn.close()
    log_audit(actor, "시스템", "긴급전환", "intake", str(intake_id), status)


def recent_intakes(phone: str, minutes: int = 10, exclude_id: int | None = None) -> list[dict]:
    """같은 번호의 최근 접수. 재전화로 생긴 중복을 사람이 알아보게 하려는 것이다.

    자동으로 합치지 않는다 — 어르신이 정말 두 번 요청했을 수도 있다.
    """
    init_db()
    conn = get_conn()
    try:
        cutoff = (datetime.datetime.now()
                  - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
        rows = conn.execute(
            "SELECT * FROM intakes WHERE phone=? AND created_at>=? "
            "AND (? IS NULL OR id<>?) ORDER BY id DESC",
            (normalize_phone(phone), cutoff, exclude_id, exclude_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# 목록 정렬 — 긴급이 맨 위, 처리가 끝난 확정은 맨 아래, 그 안에서는 최신순.
#
# 최신순만 쓰면 긴급 접수가 이후 접수들에 밀려 화면 중간에 묻힌다. 실제로
# 시연 데이터에서 긴급 한 건이 다섯 건 아래로 내려가 있었다. 이건 보기 좋고
# 나쁘고의 문제가 아니라 놓치면 안 되는 것을 놓치는 문제다.
#
# 컬럼을 i. 로 못박아 둔다 — profiles 를 조인해 나이를 끌어오는데, 거기에도
# id 컬럼이 있어서(P005…) 수식어가 없으면 SQLite 가 어느 쪽인지 모른다.
_ORDER = ("CASE i.status WHEN '긴급' THEN 0 "
          "WHEN '확정' THEN 2 WHEN '긴급 처리됨' THEN 2 ELSE 1 END, i.id DESC")


def list_intakes(limit: int = 50) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        # 목록에는 카드 전문을 싣지 않는다 — 상세는 GET /api/intakes/{id} 로 따로 받는다
        #
        # 나이와 지역만 프로필에서 끌어온다. 화면이 "박순자 · 81세" 로 부르는데
        # 접수 행에는 이름밖에 없어서, 나이를 보려면 어르신 화면으로 건너가야
        # 했다. 목록에서 급한 순서를 가리는 데 나이가 실제로 쓰인다.
        #
        # **건강 정보는 끌어오지 않는다.** 목록에 거동·독거·보호자 연락처가
        # 실리면 화면을 여는 것 자체가 개인정보 열람이 된다(profiles 목록을
        # 나눠 둔 것과 같은 이유).
        rows = conn.execute(
            f"""SELECT i.*, p.age AS target_age, p.region AS target_region
                FROM intakes i LEFT JOIN profiles p ON p.phone = i.phone
                ORDER BY {_ORDER} LIMIT ?""",
            (limit,)).fetchall()
        return [{k: v for k, v in dict(r).items() if k != "card_json"} for r in rows]
    finally:
        conn.close()


def resolve_urgent(intake_id: int, actor: str, role: str, note: str = "") -> bool:
    """긴급 건을 '처리됨'으로 내린다. 확정과는 다른 개념이다.

    확정은 동행 일정을 확정한 것이고, 이건 "사람이 연락해서 처리를 끝냈다"는
    표시다. 긴급은 접수카드를 만들지 않으므로 확정할 대상 자체가 없다.

    처리 표시를 못 하면 긴급이 목록 맨 위에 영원히 쌓여, 정작 새 긴급이
    묻힌다. 경보를 계속 켜두면 아무도 안 본다.
    """
    init_db()
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE intakes SET status='긴급 처리됨' WHERE id=? AND status='긴급'",
            (intake_id,))
        conn.commit()
        changed = cur.rowcount == 1
    finally:
        conn.close()
    if changed:
        log_audit(actor, role, "긴급처리", "intake", str(intake_id), note)
    return changed


def intake_counts() -> dict:
    """홈 대시보드 카운트 — 오늘 접수 / 접수 대기 / 확정 / 긴급."""
    init_db()
    conn = get_conn()
    try:
        today = datetime.date.today().isoformat()
        def q(sql, *a):
            return conn.execute(sql, a).fetchone()[0]
        out = {
            "today": q("SELECT COUNT(*) FROM intakes WHERE substr(created_at,1,10)=?", today),
            "waiting": q("SELECT COUNT(*) FROM intakes WHERE status='접수 대기'"),
            "confirmed": q("SELECT COUNT(*) FROM intakes WHERE status='확정'"),
            "urgent": q("SELECT COUNT(*) FROM intakes WHERE status='긴급'"),
        }
        return out
    finally:
        conn.close()


# -------------------------------------------------------------- 사후기록 --

POST_FIELDS = ("treatment", "next_visit", "pharmacy",
               "cautions", "guardian_msg", "profile_update")

# 초안 6칸 밖에서 사람이 직접 채우는 것들. AI 가 만들지 않으므로 초안
# 수정률(POST_FIELDS)의 분모에 넣지 않는다 — 넣으면 안 고쳤다는 이유로
# 수정률이 좋아진다.
POST_EXTRA = ("outcome", "depart_at", "return_at")


def save_post_record(intake_id: int, phone: str, memo_raw: str, draft: dict) -> int:
    init_db()
    conn = get_conn()
    try:
        # 초안을 그대로 한 벌 더 남긴다. 아래 컬럼들은 사회복지사가 고치면
        # 덮어써지므로, 원본이 없으면 무엇을 고쳤는지 되돌아볼 수 없다.
        snapshot = json.dumps({k: draft.get(k) for k in POST_FIELDS}, ensure_ascii=False)
        cur = conn.execute(
            """INSERT INTO post_records
               (intake_id,phone,created_at,memo_raw,treatment,next_visit,pharmacy,
                cautions,guardian_msg,profile_update,draft_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (intake_id, normalize_phone(phone), _now(), memo_raw,
             draft.get("treatment"), draft.get("next_visit"), draft.get("pharmacy"),
             draft.get("cautions"), draft.get("guardian_msg"), draft.get("profile_update"),
             snapshot))
        conn.commit()
        rid = cur.lastrowid
        return rid
    finally:
        conn.close()


def list_managers() -> list[dict]:
    """배정할 수 있는 사람 — 동행매니저 역할만.

    화면이 이름을 직접 타이핑하게 두지 않는다. 오타 하나로 '박나눔' 과
    '박나눔 매니저' 가 다른 사람이 되고, 그러면 그 사람 일정이 둘로 갈린다.
    """
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name FROM users WHERE role='동행매니저' ORDER BY name").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def assign_manager(intake_id: int, manager: str | None,
                   actor: str = "", role: str = "") -> bool:
    """동행 담당자를 배정하거나(이름) 해제한다(None).

    **확정된 접수에만 배정한다.** 아직 확정도 안 된 건에 사람을 붙이면
    매니저 일정에는 잡혀 있는데 정작 갈 병원이 안 정해진 상태가 된다.
    """
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT status, target FROM intakes WHERE id=?",
                           (intake_id,)).fetchone()
        if row is None or row["status"] != "확정":
            return False
        conn.execute("UPDATE intakes SET manager=? WHERE id=?", (manager, intake_id))
        conn.commit()
    finally:
        conn.close()
    log_audit(actor, role, "배정" if manager else "배정 해제", "intake", str(intake_id),
              f"{row['target']} → {manager or '(해제)'}")
    return True


def update_post_record(record_id: int, edits: dict) -> int:
    """승인 전에 사회복지사가 고친 내용을 반영한다. 고친 칸 수를 돌려준다.

    초안(draft_json)은 건드리지 않는다 — 그게 비교 기준이다.
    """
    allowed = POST_FIELDS + POST_EXTRA + ("saved",)
    fields = {k: v for k, v in edits.items() if k in allowed}
    if not fields:
        return 0
    init_db()
    conn = get_conn()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE post_records SET {sets} WHERE id=?",
                     (*fields.values(), record_id))
        conn.commit()
        return len(fields)
    finally:
        conn.close()


def post_record_edit_stats(record_id: int) -> dict | None:
    """초안과 현재 값을 칸별로 비교한다 — 파일1 4-2 '사후기록 초안 수정률'.

    분모는 **초안이 실제로 값을 낸 칸**이다. AI가 비워 둔 칸은 고칠 것도
    없어서, 분모에 넣으면 초안이 부실할수록 수정률이 좋아지는 뒤집힌 지표가
    된다.
    """
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM post_records WHERE id=?", (record_id,)).fetchone()
        if row is None or not row["draft_json"]:
            return None
        draft = json.loads(row["draft_json"])
        filled = [k for k in POST_FIELDS if (draft.get(k) or "").strip()]
        kept = [k for k in filled if (draft.get(k) or "") == (row[k] or "")]
        return {
            "fields": len(filled),
            "kept": len(kept),
            "edited": [k for k in filled if k not in kept],
            "kept_ratio": (len(kept) / len(filled)) if filled else None,
        }
    finally:
        conn.close()


def approve_post_record(record_id: int, approved: bool,
                        actor: str = "김○○ 사회복지사", role: str = "사회복지사") -> dict:
    """AI는 프로필을 자동 변경하지 않는다 — 승인한 항목만 반영되고 감사 로그에 남는다.

    승인 상태가 실제로 바뀔 때만 프로필에 반영한다. 예전에는 호출할 때마다
    무조건 메모를 이어 붙여서, 버튼을 두 번 누르거나 타임아웃 뒤 재요청이
    들어오면 같은 문장이 프로필에 반복해서 쌓였다("무릎 상태 악화 …;
    무릎 상태 악화 …"). 되돌릴 방법도 없다.

    이미 반영한 뒤에 거절로 바꾸는 경우, 프로필 메모는 자동으로 되돌리지
    않는다 — 그 사이 사회복지사가 직접 고쳤을 수 있어서다. 대신 감사 로그에
    수기 확인이 필요하다고 남긴다.
    """
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT phone, profile_update, approved, reviewed FROM post_records WHERE id=?",
            (record_id,)).fetchone()
        if row is None:
            return {"changed": False, "applied": False, "reason": "없는 기록"}

        want = 1 if approved else 0
        # 조건부 상태 전이 — 0→1 또는 1→0 일 때만 rowcount 가 1이다.
        # 프로필 반영은 이 전이가 있을 때만 한다(같은 메모가 두 번 붙는 것을 막는다).
        cur = conn.execute("UPDATE post_records SET approved=? WHERE id=? AND approved=?",
                           (want, record_id, 1 - want))
        changed = cur.rowcount == 1

        # **판단을 내렸다는 사실은 상태 전이와 별개다.**
        #
        # 검토 대기(approved=0) 인 초안을 거절하면 approved 는 그대로 0 이라
        # 위 UPDATE 가 아무것도 안 바꾸고, 그래서 감사 로그도 안 남았다 —
        # 사회복지사가 "보고 반영하지 않기로 했다" 고 판단한 것이 기록되지
        # 않는다는 뜻이다. 나중에 "왜 이 기록은 프로필에 없나" 를 물으면
        # 아무도 답할 수 없다.
        first_review = row["reviewed"] == 0
        conn.execute("UPDATE post_records SET reviewed=1 WHERE id=?", (record_id,))

        applied = False
        if changed and approved and row["profile_update"]:
            prev = conn.execute("SELECT notes FROM profiles WHERE phone=?", (row["phone"],)).fetchone()
            merged = "; ".join(x for x in [prev["notes"] if prev else None, row["profile_update"]] if x)
            conn.execute("UPDATE profiles SET notes=? WHERE phone=?", (merged, row["phone"]))
            applied = True
        conn.commit()
    finally:
        conn.close()

    if not changed and not first_review:
        return {"changed": False, "applied": False, "reason": "이미 같은 상태"}

    detail = row["profile_update"] or ""
    if not approved and row["approved"] == 1 and row["profile_update"]:
        detail += " (프로필 메모는 이미 반영됨 — 수기 확인 필요)"

    # 초안을 얼마나 그대로 썼는지 승인할 때마다 남긴다. 이게 쌓인 것이
    # 파일1 4-2 의 '사후기록 초안 수정률' 이다 — 나중에 따로 계산하는 게
    # 아니라 승인 한 건이 곧 표본 한 건이다.
    stats = post_record_edit_stats(record_id) if approved else None
    if stats and stats["fields"]:
        detail += (f" [초안 유지 {stats['kept']}/{stats['fields']}"
                   + (f", 수정: {', '.join(stats['edited'])}" if stats["edited"] else "")
                   + "]")

    log_audit(actor, role, "승인" if approved else "거절", "post_record", str(record_id), detail)
    return {"changed": changed, "applied": applied, "draft_kept": stats,
            "reviewed": True}


def list_post_records(limit: int = 50) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM post_records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# -------------------------------------------------------------- 감사 로그 --

def log_audit(actor: str, role: str, action: str, target_type: str,
              target_id: str, detail: str = "") -> None:
    init_db()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_log (at,actor,role,action,target_type,target_id,detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (_now(), actor, role, action, target_type, target_id, detail))
        conn.commit()
    finally:
        conn.close()


def list_audit(limit: int = 100) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# -------------------------------------------------------------- 복지시설 --

def bulk_insert_facilities(rows: list[dict]) -> int:
    init_db()
    conn = get_conn()
    try:
        conn.executemany(
            """INSERT INTO facilities (source,name,kind,region,address,phone,lat,lon)
               VALUES (:source,:name,:kind,:region,:address,:phone,:lat,:lon)""", rows)
        conn.commit()
        n = conn.total_changes
        return n
    finally:
        conn.close()


def search_facilities(region: str | None = None, kind: str | None = None,
                      keyword: str | None = None, limit: int = 20) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        sql = "SELECT * FROM facilities WHERE 1=1"
        args: list = []
        if region:
            sql += " AND region LIKE ?"
            args.append(f"%{region}%")
        if kind:
            sql += " AND kind LIKE ?"
            args.append(f"%{kind}%")
        if keyword:
            sql += " AND (name LIKE ? OR address LIKE ?)"
            args += [f"%{keyword}%"] * 2
        sql += " LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def facility_counts() -> dict[str, int]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute("SELECT source, COUNT(*) c FROM facilities GROUP BY source").fetchall()
        return {r["source"]: r["c"] for r in rows}
    finally:
        conn.close()


# ------------------------------------------------------------------ 기타 --

def _now() -> str:
    """사람이 읽는 시각 — 화면·감사 로그용. 분까지만."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _ts(when: datetime.datetime | None = None) -> str:
    """비교하는 시각 — 세션 만료용. **초까지 찍는다.**

    _now() 와 섞어 쓰면 안 된다. 둘은 자리수가 달라서 문자열 비교가
    엉킨다(접두사가 같고 길이가 다르면 짧은 쪽이 작다). 쓰는 쪽과 비교하는
    쪽이 같은 함수를 쓰게 나눠 뒀다.
    """
    return (when or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def reset_db() -> None:
    """데모 초기화 — 시드 데이터를 재적재하고 로그인 계정은 보존한다."""
    global _inited
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        # SCHEMA 에 없는 뒤늦게 늘어난 컬럼들(users.password_hash 등)을
        # 여기서도 붙인다. 빠뜨리면 init_db 없이 reset_db 를 먼저 부른
        # 프로세스가 _inited=True 를 보고 마이그레이션을 영영 건너뛴다 —
        # 그 프로세스에서 로그인을 시도하면 `no such column: password_hash` 로 죽는다.
        # (services/seed.py 의 write_and_load 가 그 호출 순서다)
        _migrate(conn)
        # 로그인 계정은 시드 데이터가 아니라 운영자 정보이므로 보존한다.
        # 세션만 지워 모든 사용자가 다시 로그인하도록 한다.
        for t in ("audit_log", "post_records", "intakes", "history", "profiles", "sessions"):
            conn.execute(f"DELETE FROM {t}")
        _seed_profiles(conn)
        conn.commit()
        _inited = True
    finally:
        conn.close()


def register_profile_from_intake(intake_id: int, actor: str, role: str) -> bool:
    """확정된 접수의 미등록 대상자를 최소 프로필로 등록한다.

    새 어르신이 케어 프로필에 들어가는 **유일한 런타임 경로**다 — 그동안은
    시드가 전부라서, 신규 접수를 확정해도 어르신 목록에 나타나지 않고
    플라이휠(이력 → 다음 접수의 후보)도 시작되지 못했다.

    확정 **후에만** 부른다. 접수 시점에 만들면 아무나 폼 한 번으로 남의
    이름을 프로필에 올릴 수 있다 — 사람이 확인·확정한 것만 기관 기록이 된다.

    조심하는 것 둘:
      · **이미 있는 프로필은 절대 덮지 않는다.** upsert 가 REPLACE 라
        재확정 한 번에 시드/기존 프로필의 건강 정보가 통째로 사라질 수 있다.
      · **자리표시 이름은 등록하지 않는다.** '신규 대상자(미등록 번호)' 가
        이름으로 박히면 목록이 쓰레기로 찬다. 사람이 확인한 실명일 때만.

    보호자 신청은 어르신 연락처를 받지 않아 **보호자 번호를 키로** 등록된다.
    notes 에 그 사실을 남긴다 — 다음에 그 번호로 오는 연락은 보호자다.
    """
    row = get_intake(intake_id)
    if not row:
        return False
    phone = (row.get("phone") or "").strip()
    name = (row.get("target") or "").strip()
    if not phone or not name:
        return False
    if any(k in name for k in ("미등록", "미확인", "신규 대상자", "후보")):
        return False
    if get_profile(phone):
        return False

    region = birth = relationship = None
    guardian = None
    raw = row.get("guardian_form_json")
    if raw:
        try:
            form = json.loads(raw)
            elder = form.get("elder") or {}
            region = (elder.get("region") or "").strip() or None
            birth = (elder.get("birthDate") or "").strip() or None
            g = form.get("guardian") or {}
            relationship = (g.get("relationship") or "").strip() or None
            if g.get("phone"):
                guardian = {"relation": relationship or "보호자",
                            "phone": normalize_phone(str(g["phone"]))}
        except (TypeError, ValueError):
            pass

    age = None
    if birth:
        try:
            y = int(birth[:4])
            age = max(0, datetime.date.today().year - y)
        except ValueError:
            pass

    channel = row.get("channel") or ""
    via_guardian = "보호자" in channel
    notes = ("보호자 신청 확정 시 자동 등록 — 이 번호는 보호자 연락처다. "
             "어르신 연락처는 미확인." if via_guardian
             else "전화 접수 확정 시 자동 등록.")

    init_db()
    conn = get_conn()
    try:
        upsert_profile(conn, normalize_phone(phone), {
            "id": f"N{intake_id:03d}",       # 시드(P001~)와 구분되는 신규 표식
            "name": name, "age": age, "region": region,
            "guardian": guardian, "notes": notes,
        })
        conn.commit()
    finally:
        conn.close()
    log_audit(actor, role, "프로필 등록", "profile", normalize_phone(phone),
              f"접수 #{intake_id} 확정에서 자동 등록 — {name}")
    return True
