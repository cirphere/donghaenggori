"""SQLite 데이터 계층 — 설치 0(파이썬 표준 sqlite3), 파일 하나(donghaenggori.db).

테이블:
  profiles  대상자(케어 프로필)
  history   과거 동행 이력 — 병원 후보 추정의 근거. source='seed' | '사후메모'(플라이휠)
  intakes   접수 기록 — 생성된 접수카드 + 사회복지사 확정 결과

최초 실행 시 data/care_profiles.json을 자동으로 시드한다.
민감 건강정보가 포함되므로 실제 운영에선 비식별·암호화 필요(시연은 합성 데이터).
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading

_ROOT = os.path.dirname(os.path.dirname(__file__))          # app/
_DATA_DIR = os.path.join(_ROOT, "data")
DB_PATH = os.path.join(_DATA_DIR, "donghaenggori.db")
_SEED = os.path.join(_DATA_DIR, "care_profiles.json")

_lock = threading.Lock()
_inited = False

_SCHEMA = """
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
  phone TEXT, date TEXT, hospital TEXT, dept TEXT, symptom TEXT,
  pharmacy INTEGER DEFAULT 0, source TEXT DEFAULT 'seed'
);
CREATE TABLE IF NOT EXISTS intakes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, phone TEXT, target TEXT,
  raw_utterance TEXT, intent TEXT,
  hospital TEXT, hospital_status TEXT, dept TEXT,
  date_value TEXT, date_label TEXT, need_level TEXT,
  confirmed INTEGER DEFAULT 0,
  confirmed_hospital TEXT, confirmed_date TEXT, confirmed_level TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(force: bool = False) -> None:
    global _inited
    with _lock:
        if _inited and not force:
            return
        conn = get_conn()
        conn.executescript(_SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
            _seed(conn)
        conn.commit()
        conn.close()
        _inited = True


def _seed(conn: sqlite3.Connection) -> None:
    with open(_SEED, encoding="utf-8") as f:
        data = json.load(f)
    for phone, p in data.items():
        if phone.startswith("_"):
            continue
        conn.execute(
            """INSERT OR REPLACE INTO profiles
               (phone,id,name,age,region,guardian_json,caregiver,mobility,
                fall_risk,lives_alone,preferred_time,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (phone, p.get("id"), p.get("name"), p.get("age"), p.get("region"),
             json.dumps(p.get("guardian"), ensure_ascii=False) if p.get("guardian") else None,
             p.get("caregiver"), p.get("mobility"),
             int(bool(p.get("fall_risk"))), int(bool(p.get("lives_alone"))),
             p.get("preferred_time"), p.get("notes")),
        )
        for h in p.get("history", []):
            conn.execute(
                """INSERT INTO history (phone,date,hospital,dept,symptom,pharmacy,source)
                   VALUES (?,?,?,?,?,?, 'seed')""",
                (phone, h.get("date"), h.get("hospital"), h.get("dept"),
                 h.get("symptom"), int(bool(h.get("pharmacy")))),
            )


# ----------------------------------------------------------------- 조회/쓰기 --

def get_profile(phone: str) -> dict | None:
    init_db()
    conn = get_conn()
    row = conn.execute("SELECT * FROM profiles WHERE phone=?", (phone,)).fetchone()
    if row is None:
        conn.close()
        return None
    hist = conn.execute(
        "SELECT date,hospital,dept,symptom,pharmacy FROM history WHERE phone=? ORDER BY date",
        (phone,)).fetchall()
    conn.close()
    return {
        "id": row["id"], "name": row["name"], "age": row["age"], "region": row["region"],
        "guardian": json.loads(row["guardian_json"]) if row["guardian_json"] else None,
        "caregiver": row["caregiver"], "mobility": row["mobility"],
        "fall_risk": bool(row["fall_risk"]), "lives_alone": bool(row["lives_alone"]),
        "preferred_time": row["preferred_time"], "notes": row["notes"],
        "history": [
            {"date": h["date"], "hospital": h["hospital"], "dept": h["dept"],
             "symptom": h["symptom"], "pharmacy": bool(h["pharmacy"])}
            for h in hist
        ],
    }


def add_history(phone, date, hospital, dept, symptom=None, pharmacy=False, source="사후메모") -> None:
    """플라이휠: 동행 완료 후 이력에 한 건 추가 → 다음 접수의 병원 후보가 더 정확해진다."""
    init_db()
    conn = get_conn()
    conn.execute(
        "INSERT INTO history (phone,date,hospital,dept,symptom,pharmacy,source) VALUES (?,?,?,?,?,?,?)",
        (phone, date, hospital, dept, symptom, int(bool(pharmacy)), source))
    conn.commit()
    conn.close()


def save_intake(card, phone: str) -> int:
    init_db()
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO intakes
           (created_at,phone,target,raw_utterance,intent,hospital,hospital_status,
            dept,date_value,date_label,need_level)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), phone, card.target,
         card.raw_utterance, card.intent, card.hospital, card.hospital_status,
         card.dept, card.date_value, card.date_label, card.need_level))
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return iid


def confirm_intake(intake_id: int, hospital: str, date: str, level: str) -> None:
    init_db()
    conn = get_conn()
    conn.execute(
        """UPDATE intakes SET confirmed=1, confirmed_hospital=?, confirmed_date=?, confirmed_level=?
           WHERE id=?""", (hospital, date, level, intake_id))
    conn.commit()
    conn.close()


def list_intakes(limit: int = 30) -> list[dict]:
    init_db()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM intakes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reset_db() -> None:
    """데모 초기화 — 모든 테이블 비우고 시드 다시 적재."""
    global _inited
    conn = get_conn()
    conn.executescript(_SCHEMA)
    for t in ("intakes", "history", "profiles"):
        conn.execute(f"DELETE FROM {t}")
    _seed(conn)
    conn.commit()
    conn.close()
    _inited = True
