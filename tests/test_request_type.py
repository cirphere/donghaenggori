"""요청 유형 분기 검증 — 기존 흐름이 못 하는 요청을 **지어내지 않고** 넘기는가.

실행:  PYTHONPATH=. python -m tests.test_request_type

여기서 지키려는 것은 하나다. 어디로 갈지 몰라서 전화한 어르신에게 **우리가 병원을
만들어 내지 않는 것.** 과거 단골을 후보로 내미는 것도, 증상에서 추정한 진료과를
카드에 앉히는 것도, 인력을 자동으로 배정하는 것도 전부 여기서 막는다.

네 층을 본다.
  분류    발화 하나가 다섯 유형 중 어디로 가는가 (Tool 단독 호출)
  회귀    시연 대본·기존 케이스가 그대로 '기존재방문' 인가
  카드    새 유형 카드에 지어낸 값이 없는가 · 왜 없는지가 근거에 남는가
  게이트  새 유형이 사람 응대 없이 확정으로 넘어가지 않는가
  조회    검증된 목록 밖의 정보를 돌려주지 않는가

fastapi 없이 돈다 — 이 경계는 API 층이 아니라 core 에 있다.
"""
from __future__ import annotations

import os
import sys
import tempfile

# db 는 임포트 시점에 경로를 읽는다. 실제 접수 DB 를 건드리지 않도록 먼저 돌려놓는다.
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="reqtype-test-"), "test.db")
os.environ["DONGHAENGGORI_DB"] = _TMP_DB

from donghaenggori.core import card as card_mod  # noqa: E402
from donghaenggori.core import db, gate, pipeline  # noqa: E402
from donghaenggori.core import requesttype as rt  # noqa: E402

PHONE_SELF = "010-1234-5678"     # 박순자 — 등록된 대상자, 정형외과 이력 있음
PHONE_NEW = "010-0000-0000"      # 미등록

# 요청하신 네 가지 새 유형 발화. 그대로 회귀로 박아 둔다.
NEW_HOSPITAL = "우리 집 주변에 새로 생긴 병원이 있어서 거기도 가보고 싶은데 좋을지 모르겠고"
SEARCH_BY_SYMPTOM = "허리가 아픈데 주변에 어떤 병원이 있는지를 모르겠어"
SEARCH_BY_DEPT = "정형외과를 가고 싶은데 정형외과가 있는 병원이 있을까"
CARE_STAFF = "다리가 불편한데 가족들은 멀리 살고, 사람이 필요해. 사람 좀 보내줄 수 있을까"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def run(utterance: str, phone: str = PHONE_SELF):
    return pipeline.run(phone, utterance, use_llm=False, with_rag=False)


def quoted(evidence: list[str]) -> list[str]:
    """근거 문장에서 따옴표로 인용된 부분만 뽑는다 — 원문 대조용."""
    out = []
    for e in evidence:
        parts = e.split("'")
        out += [parts[i] for i in range(1, len(parts), 2)]
    return out


# ------------------------------------------------------------ 분류 --

def test_classify() -> None:
    cases = [
        (NEW_HOSPITAL, "신규병원탐색"),
        (SEARCH_BY_SYMPTOM, "신규병원탐색"),
        (SEARCH_BY_DEPT, "진료과기반탐색"),
        (CARE_STAFF, "돌봄인력요청"),
        ("어떻게 해야 할지 모르겠어", "기타불분명"),
    ]
    for text, expect in cases:
        got = rt.classify(text)
        check(f"{expect} — {text[:14]}…", got.type == expect, f"→ {got.type}")

    # 판단근거는 **원문에 실제로 있던 문구**여야 한다. 요약하거나 바꿔 적으면
    # "왜 이렇게 분류됐나"에 거짓으로 답하게 된다.
    for text, _ in cases:
        got = rt.classify(text)
        missing = [q for q in quoted(got.evidence) if q not in text]
        check(f"근거가 원문 그대로 — {text[:12]}…", not missing, f"원문에 없음 {missing}")

    # 발화 텍스트만으로도 부를 수 있어야 한다(Tool 단독 호출).
    check("analysis 없이도 분류된다",
          rt.classify(SEARCH_BY_DEPT).type == "진료과기반탐색")

    # 조건 구조화 — 어르신이 말한 것만 담긴다
    c = rt.classify(SEARCH_BY_DEPT).conditions
    check("원하는 진료과를 조건으로 뽑는다", c.get("원하는진료과") == "정형외과", str(c))
    c = rt.classify(NEW_HOSPITAL).conditions
    check("위치 조건을 원문에서 뽑는다", c.get("위치조건") == "우리 집 주변", str(c))
    c = rt.classify(CARE_STAFF).conditions
    check("필요한 도움을 원문에서 뽑는다", c.get("필요한도움") == "사람 좀 보내", str(c))
    check("추정 진료과는 조건에 안 넣는다", "원하는진료과" not in c, str(c))


def test_default_is_existing() -> None:
    """애매하면 기존 흐름이다 — 시연 대본과 기존 케이스가 그대로 통과해야 한다."""
    keep = [
        "모레 정형외과 가야겄어. 저번에 무릎 봐준 데",          # 시연 장면 1
        "내일 아니고 모레 3시에 정형외과 가야 해",               # 시연 장면 2
        "병원 좀 가야 해",                                      # 시연 장면 4
        "모레 정형외과 가야하는데, 같이 가주실 분 있나요?",       # 파일3 케이스
        "허리 아파서 내일 송정병원으로 10시에 가야 될 것 같아",   # 실통화 첫 문장
        "우리 어매 병원 좀 델꼬 가야 쓰겄는디",                  # 대리 접수
        "모레 정형외과 가야하는데 혼자 못 가서요",               # 인력 표현 + 방문 계획
    ]
    for text in keep:
        got = rt.classify(text)
        check(f"기존재방문 유지 — {text[:16]}…", got.type == rt.DEFAULT, f"→ {got.type}")


# ------------------------------------------------------------ 카드 --

def test_no_invented_values() -> None:
    """새 유형 카드에는 우리가 만들어 낸 값이 없어야 한다."""
    # 박순자는 정형외과 이력이 있다. 기존 흐름이면 단골이 '추정'으로 붙는다.
    base = run("모레 정형외과 가야겄어")
    check("기존 흐름은 그대로 — 이력에서 후보를 낸다",
          base.card.hospital is not None and base.card.hospital_status == "추정",
          f"{base.card.hospital} / {base.card.hospital_status}")
    regular = base.card.hospital

    res = run(SEARCH_BY_SYMPTOM)
    c = res.card
    check("새 유형은 병원을 비운다", c.hospital is None, str(c.hospital))
    check("새 유형 병원 상태는 확인 필요", c.hospital_status == "확인 필요", c.hospital_status)
    joined = " ".join(c.fields_view()["hospital"]["evidence"])
    check("단골 이름이 근거에도 안 나온다", regular not in joined, joined)
    check("왜 비었는지가 근거에 남는다", "지어내지 않" in joined, joined)

    # 증상에서 추정한 진료과("허리" → 정형외과)를 카드에 앉히지 않는다.
    check("추정 진료과를 카드에 싣지 않는다", c.dept is None, str(c.dept))
    check("추정했다는 사실은 근거에 남는다",
          "허리" in " ".join(c.fields_view()["dept"]["evidence"]),
          str(c.fields_view()["dept"]["evidence"]))

    # 직접 말한 진료과는 그대로 남는다 — 그건 어르신이 말한 조건이다.
    c2 = run(SEARCH_BY_DEPT).card
    check("직접 말한 진료과는 확인됨", c2.fields_view()["dept"]["status"] == "확인됨",
          str(c2.fields_view()["dept"]))

    # 돌봄인력요청 — 배정할 수 있는 데이터가 없으므로 칸 자체를 세우지 않는다.
    c3 = run(CARE_STAFF).card
    fields = c3.fields_view()
    check("인력 요청에 병원 칸이 없다", "hospital" not in fields, str(list(fields)))
    check("인력 요청에 진료과 칸이 없다", "dept" not in fields, str(list(fields)))
    check("인력 요청도 요청 칸은 선다", fields["request"]["status"] == "확인 필요")

    # Inbox 배지 — 목록에서 새 유형이 갈려야 한다.
    for c4 in (c, c2, c3):
        check(f"새 유형 배지가 붙는다({c4.request_type})",
              any("새로운 유형" in f for f in c4.flags), str(c4.flags))


def test_urgent_first() -> None:
    """긴급이 먼저다 — 새 유형 분기가 안전 동작을 가리면 안 된다."""
    res = run("가슴이 답답하고 숨이 차")
    check("긴급은 카드를 만들지 않는다", res.urgent and res.card is None)
    check("긴급에는 요청 유형이 붙지 않는다", res.request is None, str(res.request))


def test_result_keys() -> None:
    d = run(SEARCH_BY_DEPT).to_dict()
    check("응답에 request_type 이 실린다", d["request_type"] == "진료과기반탐색", str(d["request_type"]))
    check("응답에 판단 근거가 실린다", bool(d["request"]["evidence"]), str(d["request"]))
    check("기존 접수는 request_type 이 기존재방문",
          run("모레 정형외과 가야겄어").to_dict()["request_type"] == rt.DEFAULT)


# ---------------------------------------------------------- 게이트 --

def test_gate_blocks() -> None:
    for text in (NEW_HOSPITAL, SEARCH_BY_DEPT, CARE_STAFF, "어떻게 해야 할지 모르겠어"):
        c = run(text).card
        g = gate.check(c.to_dict())
        blockers = {b["field"] for b in g["blockers"]}
        check(f"확정이 막힌다 — {c.request_type}", not g["allowed"], str(g["allowed"]))
        check(f"요청 칸이 막는다 — {c.request_type}", "request" in blockers, str(blockers))
        q = next((b["question"] for b in g["blockers"] if b["field"] == "request"), None)
        check(f"물어볼 질문이 붙는다 — {c.request_type}", bool(q), str(q))

    # 사유를 달고 넘어가는 기존 정책은 그대로다 — 어르신이 끊어버리는 일이 있다.
    c = run(SEARCH_BY_DEPT).card
    check("사유를 달면 넘어간다(기존 정책)",
          gate.check(c.to_dict(), acknowledge=True)["allowed"])

    # 기존 접수에는 요청 칸이 아예 없다 — 지금까지의 카드가 달라지지 않는다.
    old = run("모레 정형외과 가야겄어").card
    check("기존 카드에는 요청 칸이 없다", "request" not in old.fields_view(),
          str(list(old.fields_view())))


def test_saved_row() -> None:
    """목록·Inbox 가 카드를 열지 않고 유형을 읽을 수 있어야 한다."""
    res = run(CARE_STAFF)
    iid = db.save_intake(res.card, PHONE_SELF, "전화", status="접수 대기")
    row = db.get_intake(iid)
    check("행에 요청 유형이 남는다", row["request_type"] == "돌봄인력요청", str(row["request_type"]))
    check("기존 상태값을 건드리지 않는다", row["status"] == "접수 대기", str(row["status"]))
    check("인력을 자동 배정하지 않는다", not row["manager"], str(row["manager"]))

    old = db.save_intake(run("모레 정형외과 가야겄어").card, PHONE_SELF, "전화")
    check("기존 접수는 기존재방문으로 남는다",
          db.get_intake(old)["request_type"] == rt.DEFAULT)


# ------------------------------------------------------------ 조회 --

def test_lookup() -> None:
    """검증된 목록 밖의 정보는 돌려주지 않는다."""
    from donghaenggori.services import hira, hospital_lookup

    saved = (hira.enabled, hira.nearby)
    try:
        hira.enabled = lambda: False
        r = hospital_lookup.lookup(dept="정형외과", lat=34.9, lon=127.2)
        check("키가 없으면 후보를 만들지 않는다", r.candidates == [] and not r.available)
        check("미연동 사실을 문장으로 남긴다", "연동" in r.note, r.note)

        hira.enabled = lambda: True
        r = hospital_lookup.lookup(dept="정형외과", location_label="우리 집 주변")
        check("좌표가 없으면 조회하지 않는다", r.candidates == [] and not r.available)
        check("어디서 찾을지 모른다고 남긴다", "지역" in r.note, r.note)

        class _Res:
            unavailable = False
            data = [{"name": "○○정형외과의원", "kind": "의원", "address": "전남 고흥군 …",
                     "phone": "061-000-0000", "distance_m": 1200.0,
                     "source": "심평원 병원정보서비스", "basis": "이력 근거 없음 — 거리 기준 참고값"}]

        hira.nearby = lambda *a, **k: _Res()
        r = hospital_lookup.lookup(dept="정형외과", lat=34.9, lon=127.2)
        check("조회되면 후보가 나온다", len(r.candidates) == 1, str(r.candidates))
        got = r.candidates[0]
        check("항상 추정 후보로 표시된다", got["status"] == hospital_lookup.STATUS, got["status"])
        check("출처를 함께 싣는다", got["source"] == "심평원 병원정보서비스", str(got.get("source")))
        check("조회 결과에 없는 값을 만들지 않는다",
              set(got) <= {"name", "kind", "address", "phone", "distance_m",
                           "matched_by", "status", "source", "basis"}, str(set(got)))

        class _Empty:
            unavailable = False
            data = []

        hira.nearby = lambda *a, **k: _Empty()
        r = hospital_lookup.lookup(lat=34.9, lon=127.2)
        check("0건이면 0건이라고 말한다", r.candidates == [] and "조회되지 않" in r.note, r.note)

        def _boom(*a, **k):
            raise RuntimeError("타임아웃")

        hira.nearby = _boom
        r = hospital_lookup.lookup(lat=34.9, lon=127.2)
        check("조회 실패에도 후보를 지어내지 않는다", r.candidates == [] and not r.available, r.note)
    finally:
        hira.enabled, hira.nearby = saved


def test_card_field_sets() -> None:
    """유형별 칸 목록은 한 곳에서만 정한다(화면·게이트가 각자 세지 않도록)."""
    check("기존재방문은 의도 칸을 그대로 쓴다",
          card_mod.fields_for("병원동행", rt.DEFAULT) == card_mod.INTENT_FIELDS["병원동행"])
    check("모르는 유형은 전부 보여준다", card_mod.fields_for("병원동행", "없는유형")
          == card_mod.INTENT_FIELDS["병원동행"])
    for kind in rt.STAFF_HANDLED:
        check(f"{kind} 칸 목록에 요청 칸이 있다",
              "request" in card_mod.fields_for("병원동행", kind))


def main() -> int:
    db.init_db(force=True)
    test_classify()
    test_default_is_existing()
    test_no_invented_values()
    test_urgent_first()
    test_result_keys()
    test_gate_blocks()
    test_saved_row()
    test_lookup()
    test_card_field_sets()

    print("\n요청 유형 분기 검증")
    print("=" * 78)
    passed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:44s} {detail}")
        passed += ok
    print("=" * 78)
    print(f"  {passed}/{len(results)} 통과   (DB: {_TMP_DB})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
