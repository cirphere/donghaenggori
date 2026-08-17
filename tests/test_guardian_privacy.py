"""보호자 접수 경로가 개인정보를 흘리지 않는지 — 회귀 방지.

실행:  .venv/bin/python -m tests.test_guardian_privacy

`POST /api/guardian/intakes` 는 로그인 없이 열려 있는 유일한 쓰기 API 다.
phone 은 누구나 임의로 적을 수 있으므로, 저장된 기록에서 나온 값을 하나라도
돌려주면 그 순간 **조회 API** 가 된다 — 번호를 바꿔가며 부르면 등록된 어르신의
이름·보호자 연락처·거동 상태·독거 여부·진료 이력이 빠져나간다.

실제로 그랬다. 직원용 응답(IntakeOut)을 그대로 돌려줘서 profile 이 통째로
나왔고, card 의 근거 문장에도 이력이 들어 있었다("최근 6개월 내 ○○정형외과의원
2회 방문"). 여기 있는 검사들이 그 상태로 되돌아가는 것을 막는다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="guardian-test-"), "test.db")
os.environ["DONGHAENGGORI_DB"] = _TMP_DB

from fastapi.testclient import TestClient  # noqa: E402

from donghaenggori.core import db  # noqa: E402

# 시드 프로필(박순자)에 들어 있는 값들. 응답 어디에도 나오면 안 된다.
SECRETS = ("박순자", "이지현", "010-9876", "보행기", "낙상", "장기요양",
           "정형외과의원", "무릎 통증", "김복지", "고흥군")

REGISTERED = "010-1234-5678"     # 등록된 어르신 번호
GUARDIAN = "010-9876-5432"       # 그 어르신의 딸
UNKNOWN = "010-0000-0000"        # 미등록

results: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def leaked(payload) -> list[str]:
    blob = json.dumps(payload, ensure_ascii=False)
    return [s for s in SECRETS if s in blob]


def main() -> int:
    db.init_db(force=True)
    from donghaenggori.web.api import app
    c = TestClient(app)

    # ── 남의 번호를 넣어도 아무것도 알려주지 않는다 ─────────────────
    r = c.post("/api/guardian/intakes",
               json={"phone": REGISTERED, "utterance": "다음주 화요일에 정형외과 가야 해요"})
    check("무인증으로 접수는 된다", r.status_code == 200, f"HTTP {r.status_code}")
    d = r.json()
    check("등록 번호를 넣어도 프로필이 안 샌다", not leaked(d), str(leaked(d)))

    forbidden = [k for k in ("profile", "card", "target", "target_candidates",
                             "facilities", "intent_source", "intent_confidence")
                 if k in d]
    check("직원용 키가 응답에 없다", not forbidden, f"남은 키 {forbidden}")

    # 본인이 적어 보낸 것은 돌려줘도 된다 — 새로 알려주는 게 없다
    check("본인이 쓴 문장은 되비춘다", d.get("raw_utterance", "").startswith("다음주"))
    check("본인 문장에서 뽑은 진료과는 준다", d.get("dept") == "정형외과", str(d.get("dept")))
    check("접수 번호는 준다", isinstance(d.get("intake_id"), int), str(d.get("intake_id")))

    # ── 보호자 번호로 걸어도 마찬가지 ────────────────────────────
    # 역조회하면 "이 번호에 누가 등록돼 있는지" 를 확인해 주는 셈이 된다.
    r = c.post("/api/guardian/intakes",
               json={"phone": GUARDIAN, "utterance": "어머니 병원 좀 모시고 가야 해요"})
    check("보호자 번호 역조회 결과가 안 샌다", not leaked(r.json()), str(leaked(r.json())))

    # ── 긴급 발화도 마찬가지 ────────────────────────────────────
    r = c.post("/api/guardian/intakes",
               json={"phone": REGISTERED, "utterance": "가슴이 답답하고 숨이 차"})
    d = r.json()
    check("긴급이어도 프로필이 안 샌다", not leaked(d), str(leaked(d)))
    check("긴급 안내는 내려간다", bool(d.get("urgent_message")) if d.get("urgent") else True)

    # ── 미등록 번호 ────────────────────────────────────────────
    r = c.post("/api/guardian/intakes",
               json={"phone": UNKNOWN, "utterance": "병원 좀 가야 해요"})
    check("미등록 번호도 접수된다", r.status_code == 200, f"HTTP {r.status_code}")

    # ── channel 은 서버가 고정한다 ──────────────────────────────
    # 클라이언트가 '전화' 로 보낼 수 있으면 대리 접수 처리를 우회한다.
    r = c.post("/api/guardian/intakes",
               json={"phone": GUARDIAN, "utterance": "어머니 병원 예약", "channel": "전화"})
    iid = r.json().get("intake_id")
    row = db.get_intake(iid) if iid else None
    check("channel 을 클라이언트가 못 바꾼다",
          row and row["channel"] == "앱·웹(보호자)", row["channel"] if row else "접수 없음")

    # ── 대상자 조회는 로그인 뒤에만, 목록에는 민감정보를 안 싣는다 ────
    #
    # 여기서 나가는 것이 건강 상태·보호자 연락처·독거 여부다. 목록 한 번에
    # 스무 명 분이 통째로 나가면 화면을 여는 것 자체가 개인정보 열람이 된다.
    check("대상자 목록은 토큰 없이 401", c.get("/api/profiles").status_code == 401)
    check("대상자 상세는 토큰 없이 401",
          c.get(f"/api/profiles/{REGISTERED}").status_code == 401)

    db.create_user("T900", "테스트 복지사", "사회복지사", "pw-throwaway-only")
    auth = {"Authorization": "Bearer " + c.post(
        "/api/auth/login", json={"user_id": "T900", "password": "pw-throwaway-only"}
    ).json()["token"]}

    rows = c.get("/api/profiles", headers=auth).json()
    check("로그인하면 목록이 보인다", isinstance(rows, list) and rows, f"{len(rows)}명")
    sensitive = [k for k in ("guardian", "mobility", "fall_risk", "lives_alone",
                             "notes", "ltci_grade", "caregiver", "care_program")
                 if rows and k in rows[0]]
    check("목록에 민감정보가 없다", not sensitive, f"섞인 키 {sensitive}")

    d = c.get(f"/api/profiles/{REGISTERED}", headers=auth).json()
    check("상세에는 이력이 딸려온다", len(d.get("history") or []) > 0,
          f"{len(d.get('history') or [])}건")
    check("없는 번호는 404",
          c.get("/api/profiles/010-0000-0000", headers=auth).status_code == 404)

    # ── 보호자 조회 — 무인증인데 남의 신청이 열리면 안 된다 ──────
    #
    # 로그인 없이 여는 문이 하나 늘었다. 신청번호만 알면 열린다면 목업의
    # DH-260817-920(날짜+3자리) 처럼 대입으로 뚫린다.
    made = c.post("/api/guardian/intakes",
                  json={"phone": "010-9876-5432",
                        "utterance": "어머니 모레 정형외과 모시고 가야 해요"}).json()
    code = made.get("access_code")
    check("접수하면 신청번호를 준다", bool(code), str(code))
    check("신청번호가 추측하기 어렵다", bool(code) and len(code.split("-")[-1]) >= 8,
          f"뒷자리 {len(code.split('-')[-1]) if code else 0}글자")

    look = lambda cd, ph: c.post("/api/guardian/lookup", json={"code": cd, "phone": ph})
    r = look(code, "010-9876-5432")
    check("번호+연락처가 맞으면 열린다", r.status_code == 200, f"HTTP {r.status_code}")
    check("번호만 맞으면 안 열린다", look(code, "010-0000-0000").status_code == 404)
    check("연락처만 맞으면 안 열린다",
          look("DH-260817-AAAAAAAA", "010-9876-5432").status_code == 404)

    body = r.json()
    # 여기서 프로필·이력이 새면 신청번호를 아는 사람이 그 어르신의 병력을 알게 된다.
    for key in ("target", "profile", "card", "history", "reasons", "evidence",
                "need_basis", "guardian_contact", "target_candidates"):
        check(f"조회 응답에 {key} 가 없다", key not in body, str(body.get(key))[:40])
    check("확정 전에는 병원을 안 알려준다", body.get("hospital") is None, str(body.get("hospital")))
    check("확정 전에는 일정을 안 알려준다", body.get("date") is None, str(body.get("date")))
    check("보호자가 적어 보낸 것은 돌려준다", bool(body.get("requested")))

    # 대입 차단. 앞의 실패 2건이 이미 쌓여 있으므로 3회면 잠긴다.
    codes = [look(f"DH-260817-BAD{i:05d}", "010-9876-5432").status_code for i in range(5)]
    check("연속 실패하면 잠긴다", 429 in codes, f"{codes}")

    # ── 직원용 경로는 여전히 잠겨 있다 ──────────────────────────
    r = c.post("/api/intakes", json={"phone": REGISTERED, "utterance": "정형외과 가야 해"})
    check("직원용 접수는 토큰 없이 401", r.status_code == 401, f"HTTP {r.status_code}")
    for path in ("/api/dashboard", "/api/intakes?limit=5", "/api/audit?limit=5"):
        r = c.get(path)
        check(f"조회 {path} 는 토큰 없이 401", r.status_code == 401, f"HTTP {r.status_code}")

    print("\n보호자 접수 경로 개인정보 검증")
    print("=" * 78)
    passed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:38s} {detail}")
        passed += ok
    print("=" * 78)
    print(f"  {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
