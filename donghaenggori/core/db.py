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
DB_PATH = os.path.join(DATA_DIR, "donghaenggori.db")
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
  preferred_time TEXT, notes TEXT
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
        conn = get_conn()
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
            _seed_profiles(conn)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.executemany("INSERT INTO users (id,name,role) VALUES (?,?,?)", DEFAULT_USERS)
        conn.commit()
        conn.close()
        _inited = True


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
            fall_risk,lives_alone,preferred_time,notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (phone, p.get("id"), p.get("name"), p.get("age"), p.get("region"),
         json.dumps(p.get("guardian"), ensure_ascii=False) if p.get("guardian") else None,
         p.get("caregiver"), p.get("mobility"),
         int(bool(p.get("fall_risk"))), int(bool(p.get("lives_alone"))),
         p.get("preferred_time"), p.get("notes")))


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
    row = conn.execute("SELECT * FROM profiles WHERE phone=?", (normalize_phone(phone),)).fetchone()
    if row is None:
        conn.close()
        return None
    hist = conn.execute(
        "SELECT date,hospital,dept,symptom,pharmacy FROM history WHERE phone=? ORDER BY date",
        (row["phone"],)).fetchall()
    conn.close()
    return {
        "phone": row["phone"], "id": row["id"], "name": row["name"], "age": row["age"],
        "region": row["region"],
        "guardian": json.loads(row["guardian_json"]) if row["guardian_json"] else None,
        "caregiver": row["caregiver"], "mobility": row["mobility"],
        "fall_risk": bool(row["fall_risk"]), "lives_alone": bool(row["lives_alone"]),
        "preferred_time": row["preferred_time"], "notes": row["notes"],
        "history": [dict(h) | {"pharmacy": bool(h["pharmacy"])} for h in hist],
    }


def find_by_guardian_phone(phone: str) -> list[dict]:
    """보호자 번호로 대상자 후보를 역조회한다 (대리 접수 대응).

    보호자가 자기 폰으로 전화하면 발신번호가 대상자와 일치하지 않는다.
    이때 보호자 연락처로 등록된 대상자를 후보로 제시하되, 확정은 사람이 한다.
    """
    init_db()
    conn = get_conn()
    like = f'%"phone": "{normalize_phone(phone)}"%'
    rows = conn.execute(
        "SELECT phone, name, region, guardian_json FROM profiles WHERE guardian_json LIKE ?",
        (like,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        g = json.loads(r["guardian_json"]) if r["guardian_json"] else {}
        out.append({"phone": r["phone"], "name": r["name"], "region": r["region"],
                    "guardian_name": g.get("name"), "guardian_relation": g.get("relation")})
    return out


def add_history(phone, date, hospital, dept, symptom=None, pharmacy=False, source="사후메모") -> None:
    """동행 완료 → 이력 누적. 다음 접수의 병원 후보가 더 정확해진다(플라이휠)."""
    init_db()
    conn = get_conn()
    conn.execute(
        "INSERT INTO history (phone,date,hospital,dept,symptom,pharmacy,source) VALUES (?,?,?,?,?,?,?)",
        (normalize_phone(phone), date, hospital, dept, symptom, int(bool(pharmacy)), source))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ 접수 --

def save_intake(card, phone: str, channel: str = "전화", status: str = "접수 대기") -> int:
    init_db()
    conn = get_conn()
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
    conn.close()
    return iid


def confirm_intake(intake_id: int, hospital: str, date: str, level: str,
                   actor: str = "김○○ 사회복지사", role: str = "사회복지사") -> None:
    init_db()
    conn = get_conn()
    conn.execute(
        """UPDATE intakes SET confirmed=1, status='확정',
           confirmed_hospital=?, confirmed_date=?, confirmed_level=? WHERE id=?""",
        (hospital, date, level, intake_id))
    conn.commit()
    conn.close()
    log_audit(actor, role, "확정", "intake", str(intake_id),
              f"병원={hospital} / 방문일={date} / 지원수준={level}")


def get_intake(intake_id: int) -> dict | None:
    init_db()
    conn = get_conn()
    row = conn.execute("SELECT * FROM intakes WHERE id=?", (intake_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_intakes(limit: int = 50) -> list[dict]:
    init_db()
    conn = get_conn()
    rows = conn.execute("SELECT * FROM intakes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def intake_counts() -> dict:
    """홈 대시보드 카운트 — 오늘 접수 / 접수 대기 / 확정 / 긴급."""
    init_db()
    conn = get_conn()
    today = datetime.date.today().isoformat()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    out = {
        "today": q("SELECT COUNT(*) FROM intakes WHERE substr(created_at,1,10)=?", today),
        "waiting": q("SELECT COUNT(*) FROM intakes WHERE status='접수 대기'"),
        "confirmed": q("SELECT COUNT(*) FROM intakes WHERE status='확정'"),
        "urgent": q("SELECT COUNT(*) FROM intakes WHERE status='긴급'"),
    }
    conn.close()
    return out


# -------------------------------------------------------------- 사후기록 --

def save_post_record(intake_id: int, phone: str, memo_raw: str, draft: dict) -> int:
    init_db()
    conn = get_conn()
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
    conn.close()
    return rid


def approve_post_record(record_id: int, approved: bool,
                        actor: str = "김○○ 사회복지사", role: str = "사회복지사") -> None:
    """AI는 프로필을 자동 변경하지 않는다 — 승인한 항목만 반영되고 감사 로그에 남는다."""
    init_db()
    conn = get_conn()
    conn.execute("UPDATE post_records SET approved=? WHERE id=?", (1 if approved else 0, record_id))
    row = conn.execute("SELECT phone, profile_update FROM post_records WHERE id=?", (record_id,)).fetchone()
    if approved and row and row["profile_update"]:
        prev = conn.execute("SELECT notes FROM profiles WHERE phone=?", (row["phone"],)).fetchone()
        merged = "; ".join(x for x in [prev["notes"] if prev else None, row["profile_update"]] if x)
        conn.execute("UPDATE profiles SET notes=? WHERE phone=?", (merged, row["phone"]))
    conn.commit()
    conn.close()
    log_audit(actor, role, "승인" if approved else "거절", "post_record", str(record_id),
              (row["profile_update"] if row else "") or "")


def list_post_records(limit: int = 50) -> list[dict]:
    init_db()
    conn = get_conn()
    rows = conn.execute("SELECT * FROM post_records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------- 감사 로그 --

def log_audit(actor: str, role: str, action: str, target_type: str,
              target_id: str, detail: str = "") -> None:
    init_db()
    conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log (at,actor,role,action,target_type,target_id,detail) VALUES (?,?,?,?,?,?,?)",
        (_now(), actor, role, action, target_type, target_id, detail))
    conn.commit()
    conn.close()


def list_audit(limit: int = 100) -> list[dict]:
    init_db()
    conn = get_conn()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------- 복지시설 --

def bulk_insert_facilities(rows: list[dict]) -> int:
    init_db()
    conn = get_conn()
    conn.executemany(
        """INSERT INTO facilities (source,name,kind,region,address,phone,lat,lon)
           VALUES (:source,:name,:kind,:region,:address,:phone,:lat,:lon)""", rows)
    conn.commit()
    n = conn.total_changes
    conn.close()
    return n


def search_facilities(region: str | None = None, kind: str | None = None,
                      keyword: str | None = None, limit: int = 20) -> list[dict]:
    init_db()
    conn = get_conn()
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
    conn.close()
    return [dict(r) for r in rows]


def facility_counts() -> dict[str, int]:
    init_db()
    conn = get_conn()
    rows = conn.execute("SELECT source, COUNT(*) c FROM facilities GROUP BY source").fetchall()
    conn.close()
    return {r["source"]: r["c"] for r in rows}


# ------------------------------------------------------------------ 기타 --

def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def reset_db() -> None:
    """데모 초기화 — 모든 테이블 비우고 시드 재적재(복지시설 제외)."""
    global _inited
    conn = get_conn()
    conn.executescript(SCHEMA)
    for t in ("audit_log", "post_records", "intakes", "history", "profiles", "users"):
        conn.execute(f"DELETE FROM {t}")
    _seed_profiles(conn)
    conn.executemany("INSERT INTO users (id,name,role) VALUES (?,?,?)", DEFAULT_USERS)
    conn.commit()
    conn.close()
    _inited = True
