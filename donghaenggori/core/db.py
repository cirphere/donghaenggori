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
  confirmed_hospital TEXT, confirmed_date TEXT, confirmed_level TEXT,
  identity_answer TEXT,   -- 전화에서 '맞으실까요' 에 답한 내용 (원문 그대로)
  identity_status TEXT,   -- 확인됨 | 추정 | 확인 필요 — 확정은 사람이 한다
  card_json TEXT,         -- 접수 당시 생성된 카드 전문(근거·확인질문 포함)
  transfer_status TEXT    -- 긴급 전환 결과 (연결됨 | 통화중 | 응답없음 | 실패)
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
    ("intakes", "identity_answer", "TEXT"),
    ("intakes", "identity_status", "TEXT"),
    ("intakes", "card_json", "TEXT"),
    ("intakes", "transfer_status", "TEXT"),
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
                dept,date_value,date_label,need_level,status,card_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), channel, normalize_phone(phone), card.target, card.raw_utterance, card.intent,
             card.hospital, card.hospital_status, card.dept, card.date_value, card.date_label,
             card.need_level, status, _card_json(card)))
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
    "time":     ("time_value", None),      # 시각은 컬럼이 없다 — 카드에만 있다
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
_ORDER = ("CASE status WHEN '긴급' THEN 0 "
          "WHEN '확정' THEN 2 WHEN '긴급 처리됨' THEN 2 ELSE 1 END, id DESC")


def list_intakes(limit: int = 50) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        # 목록에는 카드 전문을 싣지 않는다 — 상세는 GET /api/intakes/{id} 로 따로 받는다
        rows = conn.execute(
            f"SELECT * FROM intakes ORDER BY {_ORDER} LIMIT ?", (limit,)).fetchall()
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
