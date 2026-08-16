"""접수 확정 게이트 검증 — "확정은 사람" 이 실제로 강제되는지.

실행:  .venv/bin/python -m tests.test_gate

세 층을 본다.
  규칙   무엇이 막고 무엇이 안 막는가 (합성 카드로 경계를 찍는다)
  파이프라인  실제 발화로 만든 카드가 의도대로 막히는가
  API    409 로 막히고, 확인 입력으로 풀리고, 사유를 달면 넘어가는가
"""
from __future__ import annotations

import os
import sys
import tempfile

# db 는 임포트 시점에 DB 경로를 읽는다. 실제 접수 DB 를 건드리지 않도록
# **다른 모듈보다 먼저** 임시 경로로 돌려놓는다.
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="gate-test-"), "test.db")
os.environ["DONGHAENGGORI_DB"] = _TMP_DB
os.environ.pop("INTAKE_BLOCK_ALL_UNCONFIRMED", None)

from fastapi.testclient import TestClient  # noqa: E402

from donghaenggori.core import db, gate, pipeline  # noqa: E402

PHONE_SELF = "010-1234-5678"     # 박순자 — 등록된 대상자
PHONE_NEW = "010-0000-0000"      # 미등록

results: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def card(**spec) -> dict:
    """합성 카드. spec 은 항목이름=(값, 상태) 또는 항목이름=(값, 상태, 말한표현)."""
    fields = {}
    for name, t in spec.items():
        value, status = t[0], t[1]
        f = {"label": name, "value": value, "status": status, "evidence": []}
        if len(t) > 2:
            f["spoken"] = t[2]
        fields[name] = f
    return {"fields": fields, "confirm_questions": []}


def labels(g: dict) -> set[str]:
    return {b["field"] for b in g["blockers"]}


# ------------------------------------------------------------ 규칙 --

def test_rules() -> None:
    ok = card(target=("박순자", "확인됨"), hospital=("고흥정형외과", "확인됨"),
              date=("2026-08-18", "확인됨"), time=("15:00", "확인됨"))
    check("전부 확인됨 → 확정 가능", gate.check(ok)["allowed"])

    g = gate.check(card(target=("박순자", "확인됨"), hospital=(None, "확인 필요"),
                        date=("2026-08-18", "확인됨")))
    check("병원 확인 필요 → 막힘", not g["allowed"] and labels(g) == {"hospital"},
          f"막은 항목 {labels(g)}")

    # 진료과는 일정 성립에 필요 없다 — 비어 있어도 확정할 수 있어야 한다
    g = gate.check(card(target=("박순자", "확인됨"), hospital=("고흥정형외과", "확인됨"),
                        date=("2026-08-18", "확인됨"), dept=(None, "확인 필요")))
    check("진료과 확인 필요 → 안 막음", g["allowed"], f"막은 항목 {labels(g)}")

    # 추정까지 막으면 거의 모든 카드가 걸려 3단계가 2단계로 무너진다
    g = gate.check(card(target=("박순자", "추정"), hospital=("고흥정형외과", "추정"),
                        date=("2026-08-18", "확인됨")))
    check("추정 → 안 막음", g["allowed"], f"막은 항목 {labels(g)}")

    # 시각: 말한 적 없으면 통과, 말했는데 모호하면 막는다.
    # 모호한 "3시"는 해석에 실패해 value 가 None 이고 spoken 에만 남는다 —
    # 그래서 value 가 아니라 spoken 으로 갈라야 한다.
    base = {"target": ("박순자", "확인됨"), "hospital": ("고흥정형외과", "확인됨"),
            "date": ("2026-08-18", "확인됨")}
    check("시각 말한 적 없음 → 안 막음",
          gate.check(card(**base, time=(None, "확인 필요")))["allowed"])
    g = gate.check(card(**base, time=(None, "확인 필요", "3시")))
    check("시각 모호(3시, 값 없음) → 막힘", not g["allowed"] and labels(g) == {"time"},
          f"막은 항목 {labels(g)}")

    # 날짜는 없어도 막는다 — 날짜 없는 일정은 세울 수 없다
    g = gate.check(card(target=("박순자", "확인됨"), hospital=("고흥정형외과", "확인됨"),
                        date=(None, "확인 필요")))
    check("날짜 없음 → 막힘", not g["allowed"] and labels(g) == {"date"})

    # 말한 성함·주소는 별도 항목이 아니라 대상자 블로커의 '들은 말'이다
    g = gate.check(card(target=(None, "확인 필요"), spoken_name=("김말자", "확인 필요"),
                        spoken_region=("도양읍", "확인 필요"),
                        hospital=("고흥정형외과", "확인됨"), date=("2026-08-20", "확인됨")))
    heard = {h["value"] for h in g["blockers"][0].get("heard", [])}
    check("말한 성함·주소 → 별도 항목 아님", labels(g) == {"target"}, f"막은 항목 {labels(g)}")
    check("말한 성함·주소 → 대상자 블로커에 동봉", heard == {"김말자", "도양읍"}, f"들은 말 {heard}")

    # 카드 없는 접수(긴급)는 확정 대상이 아니다 — 막을 것도 없다
    check("카드 없음 → 막지 않음", gate.check(None)["allowed"])

    # 사유를 달면 넘어간다 / 기관 규칙을 켜면 사유도 안 통한다
    blocked = card(target=("박순자", "확인됨"), hospital=(None, "확인 필요"),
                   date=("2026-08-18", "확인됨"))
    check("acknowledge → 통과", gate.check(blocked, acknowledge=True)["allowed"])
    os.environ["INTAKE_BLOCK_ALL_UNCONFIRMED"] = "true"
    g = gate.check(blocked, acknowledge=True)
    check("기관 규칙 ON → acknowledge 무시", not g["allowed"] and g["hard_block"])
    check("기관 규칙 ON → 확인되면 통과", gate.check(ok, acknowledge=False)["allowed"])
    os.environ.pop("INTAKE_BLOCK_ALL_UNCONFIRMED")

    # 되묻는 질문이 항목에 붙어야 통화에서 바로 쓴다
    c = card(target=("박순자", "확인됨"), hospital=(None, "확인 필요"),
             date=("2026-08-18", "확인됨"))
    c["confirm_questions"] = ["어르신, 어느 병원으로 모실지 확인 부탁드립니다."]
    g = gate.check(c)
    check("블로커에 추천 질문이 붙는다", "병원" in (g["blockers"][0]["question"] or ""),
          repr(g["blockers"][0]["question"]))


# ------------------------------------------------------- 파이프라인 --

def test_pipeline() -> None:
    res = pipeline.run(PHONE_SELF, "모레 3시에 정형외과 가야 해")
    g = gate.check(res.to_dict().get("card"))
    check("실제 발화 '모레 3시' → 시각 때문에 막힘",
          not g["allowed"] and "time" in labels(g), f"막은 항목 {labels(g)}")

    res = pipeline.run(PHONE_NEW, "병원 좀 가야 해. 다리가 아파서.")
    g = gate.check(res.to_dict().get("card"))
    check("미등록 발화 → 대상자·병원이 막힘",
          not g["allowed"] and {"target", "hospital"} <= labels(g), f"막은 항목 {labels(g)}")


# --------------------------------------------------------------- API --

TEST_PASSWORD = "test-pw-only-throwaway-db"     # 임시 DB 전용 — 실운영 값 아님


def test_api() -> None:
    from donghaenggori.web.api import app
    client = TestClient(app)

    # 임시 tempdir DB에만 존재하는 테스트 전용 계정 — 실제 신원 확인 경로를 탄다.
    db.create_user("T001", "테스트 사회복지사", "사회복지사", "test-sw@local", TEST_PASSWORD)
    db.create_user("T002", "테스트 동행매니저", "동행매니저", "test-mgr@local", TEST_PASSWORD)
    r = client.post("/api/auth/login", json={"email": "test-sw@local", "password": TEST_PASSWORD})
    check("사회복지사 로그인 성공", r.status_code == 200, f"HTTP {r.status_code}")
    AUTH = {"Authorization": f"Bearer {r.json()['token']}"}
    r = client.post("/api/auth/login", json={"email": "test-mgr@local", "password": TEST_PASSWORD})
    AUTH_MGR = {"Authorization": f"Bearer {r.json()['token']}"}

    # 토큰 없이 접수 생성 시도 → 401 (직원용 엔드포인트는 이제 로그인 필요)
    r = client.post("/api/intakes", json={"phone": PHONE_SELF, "utterance": "테스트"})
    check("토큰 없이 접수 생성 → 401", r.status_code == 401, f"HTTP {r.status_code}")

    r = client.post("/api/intakes", json={"phone": PHONE_SELF,
                                          "utterance": "모레 3시에 정형외과 가야 해"},
                    headers=AUTH)
    body = r.json()
    iid = body["intake_id"]
    check("접수 응답에 gate 가 실린다", body.get("gate") is not None
          and not body["gate"]["allowed"], str(body.get("gate", {}).get("allowed")))

    r = client.get(f"/api/intakes/{iid}", headers=AUTH)
    check("상세 조회에도 gate 가 실린다", not r.json()["gate"]["allowed"])

    # 보호자 웹 경로는 토큰 없이도 접수가 만들어져야 한다 — channel은 서버가 고정
    r = client.post("/api/guardian/intakes",
                    json={"phone": PHONE_SELF, "utterance": "모레 정형외과 가야겄어"})
    check("보호자 접수는 토큰 없이도 성공", r.status_code == 200, f"HTTP {r.status_code}")
    giid = r.json()["intake_id"]
    r2 = client.get(f"/api/intakes/{giid}", headers=AUTH)
    check("보호자 접수의 channel이 고정된다", r2.json()["channel"] == "앱·웹(보호자)",
          r2.json().get("channel"))

    payload = {"hospital": "고흥정형외과", "date": "2026-08-16", "level": "부축 동행"}
    r = client.post(f"/api/intakes/{iid}/confirm", json=payload, headers=AUTH)
    check("확인 필요가 남으면 확정이 409", r.status_code == 409, f"HTTP {r.status_code}")
    detail = r.json()["detail"]
    check("409 본문이 막은 항목을 담는다",
          any(b["field"] == "time" for b in detail["gate"]["blockers"]),
          str([b["field"] for b in detail["gate"]["blockers"]]))
    check("확정되지 않았다",
          client.get(f"/api/intakes/{iid}", headers=AUTH).json()["confirmed"] == 0)

    # 토큰 없이는 401 — 인증 자체가 걸려 있는지
    r = client.post(f"/api/intakes/{iid}/confirm", json=payload)
    check("토큰 없이 확정 시도 → 401", r.status_code == 401, f"HTTP {r.status_code}")

    # 통화로 확인 → 게이트가 풀린다
    r = client.post(f"/api/intakes/{iid}/verify", json={"field": "time", "value": "15:00"},
                    headers=AUTH)
    check("확인 입력 성공", r.status_code == 200, f"HTTP {r.status_code}")
    g = r.json()["intake"]["gate"]
    check("확인 입력으로 게이트가 풀린다", g["allowed"], f"남은 항목 {labels(g)}")
    ev = r.json()["intake"]["card"]["fields"]["time"]["evidence"]
    check("근거에 '통화로 확인함' 이 남는다", any("통화로 확인함" in e for e in ev), str(ev))

    r = client.post(f"/api/intakes/{iid}/confirm", json=payload, headers=AUTH)
    check("풀린 뒤 확정 성공", r.status_code == 200 and r.json()["intake"]["confirmed"] == 1,
          f"HTTP {r.status_code}")
    check("사유 없이 확정 → acknowledged=False", r.json()["acknowledged"] is False)

    # 사유를 달고 넘어가기 — 감사 로그에 남아야 한다
    r = client.post("/api/intakes", json={"phone": PHONE_NEW,
                                          "utterance": "병원 좀 가야 해. 다리가 아파서."},
                    headers=AUTH)
    iid2 = r.json()["intake_id"]
    r = client.post(f"/api/intakes/{iid2}/confirm", json=payload, headers=AUTH)
    check("미등록 건도 409 로 막힌다", r.status_code == 409, f"HTTP {r.status_code}")
    r = client.post(f"/api/intakes/{iid2}/confirm", json={**payload, "acknowledge": True},
                    headers=AUTH)
    check("acknowledge=true 면 확정된다", r.status_code == 200 and r.json()["acknowledged"],
          f"HTTP {r.status_code}")
    actions = [a["action"] for a in db.list_audit(limit=20)]
    check("'미확인 확정' 이 감사 로그에 남는다", "미확인 확정" in actions, str(actions[:4]))

    # 대상자를 확인하면 '말한 성함' 칸은 사라진다
    r = client.post("/api/intakes", json={"phone": PHONE_NEW,
                                          "utterance": "병원 좀 가야 해. 다리가 아파서."},
                    headers=AUTH)
    iid3 = r.json()["intake_id"]
    r = client.post(f"/api/intakes/{iid3}/verify", json={"field": "target", "value": "김말자"},
                    headers=AUTH)
    fields = r.json()["intake"]["card"]["fields"]
    check("대상자 확인 → 확인됨", fields["target"]["status"] == "확인됨")
    check("대상자 확인 → 말한 성함 칸 제거", "spoken_name" not in fields, str(list(fields)))

    r = client.post(f"/api/intakes/{iid3}/verify", json={"field": "존재없음", "value": "x"},
                    headers=AUTH)
    check("모르는 항목은 422", r.status_code == 422, f"HTTP {r.status_code}")

    r = client.post("/api/intakes/999999/verify", json={"field": "time", "value": "15:00"},
                    headers=AUTH)
    check("없는 접수는 404", r.status_code == 404, f"HTTP {r.status_code}")

    # 권한 없는 역할(동행매니저)로는 확인 입력이 403 — role은 더 이상 본문이 아니라 토큰으로 정해진다
    r = client.post(f"/api/intakes/{iid}/verify", json={"field": "time", "value": "15:00"},
                    headers=AUTH_MGR)
    check("권한 없는 역할은 403", r.status_code == 403, f"HTTP {r.status_code}")


def main() -> int:
    db.init_db(force=True)
    test_rules()
    test_pipeline()
    test_api()

    print("\n접수 확정 게이트 검증")
    print("=" * 78)
    passed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:38s} {detail}")
        passed += ok
    print("=" * 78)
    print(f"  {passed}/{len(results)} 통과   (DB: {_TMP_DB})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
