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
import json
import os
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
  care_program TEXT       -- 노인맞춤돌봄서비스 군 (지자체 선정)
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
  need_level TEXT,
  status TEXT DEFAULT '접수 대기',        -- 접수 대기 | 확정 | 임시 접수 | 긴급
  confirmed INTEGER DEFAULT 0,
  confirmed_hospital TEXT, confirmed_date TEXT, confirmed_level TEXT
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
  approved INTEGER DEFAULT 0              -- 사회복지사 승인 여부
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

CREATE TABLE IF NOT EXISTS facilities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT,                            -- C-DS03 | C-DS04 | C-DS06 | C-DS17 ...
  name TEXT, kind TEXT, region TEXT,
  address TEXT, phone TEXT,
  lat REAL, lon REAL
);
CREATE INDEX IF NOT EXISTS idx_fac_region ON facilities(region);
"""

DEFAULT_USERS = [
    ("U001", "김○○ 사회복지사", "사회복지사"),
    ("U002", "최정미 동행매니저", "동행매니저"),
]

# 역할별 권한 — 화면 01의 "권한: 접수 확정·수정 (RBAC)"
ROLE_PERMISSIONS = {
    "사회복지사": {"intake.view", "intake.confirm", "intake.edit", "post.approve", "audit.view"},
    "동행매니저": {"intake.view", "post.write"},
    "관리자": {"intake.view", "intake.confirm", "intake.edit", "post.approve", "audit.view", "admin"},
}


def can(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())


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
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                conn.executemany("INSERT INTO users (id,name,role) VALUES (?,?,?)", DEFAULT_USERS)
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
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
            fall_risk,lives_alone,preferred_time,notes,ltci_grade,care_program)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (phone, p.get("id"), p.get("name"), p.get("age"), p.get("region"),
         json.dumps(p.get("guardian"), ensure_ascii=False) if p.get("guardian") else None,
         p.get("caregiver"), p.get("mobility"),
         int(bool(p.get("fall_risk"))), int(bool(p.get("lives_alone"))),
         p.get("preferred_time"), p.get("notes"),
         p.get("ltci_grade"), p.get("care_program")))


# ------------------------------------------------------------- 프로필/이력 --

def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
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
            "region": row["region"],
            "guardian": json.loads(row["guardian_json"]) if row["guardian_json"] else None,
            "caregiver": row["caregiver"], "mobility": row["mobility"],
            "fall_risk": bool(row["fall_risk"]), "lives_alone": bool(row["lives_alone"]),
            "preferred_time": row["preferred_time"], "notes": row["notes"],
            "ltci_grade": row["ltci_grade"], "care_program": row["care_program"],
            "history": [dict(h) | {"pharmacy": bool(h["pharmacy"])} for h in hist],
        }
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

def save_intake(card, phone: str, channel: str = "전화", status: str = "접수 대기") -> int:
    init_db()
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO intakes
               (created_at,channel,phone,target,raw_utterance,intent,hospital,hospital_status,
                dept,date_value,date_label,need_level,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), channel, normalize_phone(phone), card.target, card.raw_utterance, card.intent,
             card.hospital, card.hospital_status, card.dept, card.date_value, card.date_label,
             card.need_level, status))
        conn.commit()
        iid = cur.lastrowid
        return iid
    finally:
        conn.close()


def confirm_intake(intake_id: int, hospital: str, date: str, level: str,
                   actor: str = "김○○ 사회복지사", role: str = "사회복지사") -> None:
    init_db()
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE intakes SET confirmed=1, status='확정',
               confirmed_hospital=?, confirmed_date=?, confirmed_level=? WHERE id=?""",
            (hospital, date, level, intake_id))
        conn.commit()
        log_audit(actor, role, "확정", "intake", str(intake_id),
                  f"병원={hospital} / 방문일={date} / 지원수준={level}")
    finally:
        conn.close()


def get_intake(intake_id: int) -> dict | None:
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM intakes WHERE id=?", (intake_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_intakes(limit: int = 50) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM intakes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def intake_counts() -> dict:
    """홈 대시보드 카운트 — 오늘 접수 / 접수 대기 / 확정 / 긴급."""
    init_db()
    conn = get_conn()
    try:
        today = datetime.date.today().isoformat()
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
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

def save_post_record(intake_id: int, phone: str, memo_raw: str, draft: dict) -> int:
    init_db()
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO post_records
               (intake_id,phone,created_at,memo_raw,treatment,next_visit,pharmacy,
                cautions,guardian_msg,profile_update)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (intake_id, normalize_phone(phone), _now(), memo_raw,
             draft.get("treatment"), draft.get("next_visit"), draft.get("pharmacy"),
             draft.get("cautions"), draft.get("guardian_msg"), draft.get("profile_update")))
        conn.commit()
        rid = cur.lastrowid
        return rid
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
            "SELECT phone, profile_update, approved FROM post_records WHERE id=?",
            (record_id,)).fetchone()
        if row is None:
            return {"changed": False, "applied": False, "reason": "없는 기록"}

        want = 1 if approved else 0
        # 조건부 상태 전이 — 0→1 또는 1→0 일 때만 rowcount 가 1이다
        cur = conn.execute("UPDATE post_records SET approved=? WHERE id=? AND approved=?",
                           (want, record_id, 1 - want))
        changed = cur.rowcount == 1

        applied = False
        if changed and approved and row["profile_update"]:
            prev = conn.execute("SELECT notes FROM profiles WHERE phone=?", (row["phone"],)).fetchone()
            merged = "; ".join(x for x in [prev["notes"] if prev else None, row["profile_update"]] if x)
            conn.execute("UPDATE profiles SET notes=? WHERE phone=?", (merged, row["phone"]))
            applied = True
        conn.commit()
    finally:
        conn.close()

    if not changed:
        return {"changed": False, "applied": False, "reason": "이미 같은 상태"}

    detail = row["profile_update"] or ""
    if not approved and row["approved"] == 1 and row["profile_update"]:
        detail += " (프로필 메모는 이미 반영됨 — 수기 확인 필요)"
    log_audit(actor, role, "승인" if approved else "거절", "post_record", str(record_id), detail)
    return {"changed": True, "applied": applied}


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
            "INSERT INTO audit_log (at,actor,role,action,target_type,target_id,detail) VALUES (?,?,?,?,?,?,?)",
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
            sql += " AND region LIKE ?"; args.append(f"%{region}%")
        if kind:
            sql += " AND kind LIKE ?"; args.append(f"%{kind}%")
        if keyword:
            sql += " AND (name LIKE ? OR address LIKE ?)"; args += [f"%{keyword}%"] * 2
        sql += " LIMIT ?"; args.append(limit)
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
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def reset_db() -> None:
    """데모 초기화 — 모든 테이블 비우고 시드 재적재(복지시설 제외)."""
    global _inited
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        for t in ("audit_log", "post_records", "intakes", "history", "profiles", "users"):
            conn.execute(f"DELETE FROM {t}")
        _seed_profiles(conn)
        conn.executemany("INSERT INTO users (id,name,role) VALUES (?,?,?)", DEFAULT_USERS)
        conn.commit()
        _inited = True
    finally:
        conn.close()
