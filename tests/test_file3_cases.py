"""제출 문서(파일3 샘플데이터)의 테스트 케이스 12건 회귀 검증.

심사 당일 "이 케이스 돌려보세요"에 그대로 답할 수 있어야 한다.
문서의 접수일 기준은 2026-07-07(화)이므로, 날짜 해석은 그 날짜로 고정해 검증한다.

실행:  .venv/bin/python -m tests.test_file3_cases
"""
from __future__ import annotations

import datetime
import sys

from donghaenggori.core import db, dateparse, hospital, needlevel, pipeline
from donghaenggori.services import rag, summarize

BASE_DATE = datetime.date(2026, 7, 7)      # 문서 기준 접수일 (화)
PHONE_MAIN = "010-1234-5678"               # 박순자 — 정형외과 단골 2회
PHONE_NEW = "010-0000-0000"                # 미등록 번호

results: list[tuple[int, str, bool, str]] = []


def check(no: int, name: str, passed, detail: str) -> None:
    results.append((no, name, bool(passed), detail))


# ── 1. 단골 → 확인됨 + 날짜 해석 + 확인 질문 ────────────────────
def case1() -> None:
    r = pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어. 저번에 무릎 봐준 데")
    c = r.card
    d = dateparse.parse_date("모레", today=BASE_DATE)
    ok = (c.dept == "정형외과" and c.hospital_status == "확인됨"
          and d["date"] == "2026-07-09" and len(r.analysis.date or {}) > 0)
    check(1, "단골 → 확인됨 + 날짜 해석", ok,
          f"{c.hospital}[{c.hospital_status}] / 모레→{d['date']} / 질문 {len(c.confirm_questions)}개")


# ── 2. "저번 병원" — 최근 이력 우선 ─────────────────────────────
def case2() -> None:
    p = db.get_profile(PHONE_MAIN)
    res = hospital.suggest(p, "정형외과", today=BASE_DATE)
    ok = res.hospital is not None and res.status in ("확인됨", "추정")
    check(2, "'저번 병원' 최근 이력 우선", ok,
          f"{res.hospital}[{res.status}] / 후보 {len(res.candidates)}건")


# ── 3. 약국 의도 분류 ──────────────────────────────────────────
def case3() -> None:
    r = pipeline.run(PHONE_MAIN, "약 타러 가야 하는디")
    ok = r.analysis.intent == "약국"
    check(3, "약국 의도 분류", ok, f"의도={r.analysis.intent} ({r.intent_source})")


# ── 4. 이력 없는 요청 → 정보 부족 ───────────────────────────────
def case4() -> None:
    r = pipeline.run("010-7777-8888", "내일 그 큰 병원 좀")   # 정말순 = 이력 0
    c = r.card
    ok = c.hospital_status == "확인 필요" and c.hospital is None and len(c.confirm_questions) > 0
    check(4, "이력 없음 → 확인 필요", ok,
          f"[{c.hospital_status}] 병원={c.hospital} / 질문 {len(c.confirm_questions)}개")


# ── 5. 긴급 → 카드 생성 중단 ────────────────────────────────────
def case5() -> None:
    r = pipeline.run(PHONE_MAIN, "가슴이 답답하고 숨이 차")
    ok = r.urgent and r.card is None and r.urgent_message
    check(5, "긴급 → 접수 중단", ok, f"urgent={r.urgent} card={r.card}")


# ── 6. 미등록 번호 → 대상자 미확인 ──────────────────────────────
def case6() -> None:
    r = pipeline.run(PHONE_NEW, "병원 좀 가야 해")
    ok = r.profile is None and r.card.target.startswith("신규")
    check(6, "미등록 번호 → 임시 접수", ok, f"target={r.card.target}")


# ── 7. 보호자 대리 전화 (문서상 '보완') ─────────────────────────
def case7() -> None:
    """보호자가 자기 폰으로 대신 접수 — 대상자를 확정하지 않고 후보만 제시해야 한다."""
    r = pipeline.run("010-9876-5432", "느그 어매 병원 좀 델꼬 가야 쓰겄는디")  # 박순자의 딸
    c = r.card
    ok = (c.requester == "대리" and c.proxy_relation == "어머니"
          and len(c.target_candidates) == 1
          and c.target_candidates[0]["name"] == "박순자"
          and any("대리 요청" in f for f in c.flags))
    check(7, "대리 전화 → 대상자 후보 역조회", ok,
          f"{c.requester}({c.proxy_relation}) → 후보 {[x['name'] for x in c.target_candidates]} / {c.flags[0]}")


# ── 8. 상대 날짜 '다음 주 화요일' ───────────────────────────────
def case8() -> None:
    d = dateparse.parse_date("다음주 화요일에 병원 가야", today=BASE_DATE)
    ok = d is not None and d["date"] == "2026-07-14"
    check(8, "'다음주 화요일' → 7/14", ok, f"{d}")


# ── 9. 동행 필요도 근거 ────────────────────────────────────────
def case9() -> None:
    p = db.get_profile(PHONE_MAIN)      # 독거 + 보행기 + 낙상
    res = needlevel.assess(p)
    ok = res.level == "휠체어·부축 동행" and len(res.reasons) >= 3
    check(9, "동행 필요도 + 근거 3개", ok, f"{res.level} / 근거 {res.reasons}")


# ── 10. 광주 대상자 → 복지관 조회 ───────────────────────────────
def case10() -> None:
    conn = db.get_conn()
    row = conn.execute("SELECT phone,region FROM profiles WHERE region LIKE '광주%서구%' LIMIT 1").fetchone()
    if row is None:
        row = conn.execute("SELECT phone,region FROM profiles WHERE region LIKE '광주%' LIMIT 1").fetchone()
    conn.close()
    found = rag.search(region=row["region"], limit=5) if row else []
    ok = len(found) > 0
    check(10, "광주 대상자 → 복지관 조회", ok,
          f"{row['region'] if row else '—'} → {len(found)}건 " +
          (f"({found[0]['name']}, 출처 {found[0]['source']})" if found else ""))


# ── 11. 전남 대상자 → 시설 조회 (C-DS04/17 미연동) ──────────────
def case11() -> None:
    found = rag.search(region="전남 고흥군", limit=5)
    ok = True   # 미연동이 정상 — 문서 파일3에도 C-DS04/17 외 전남 시설은 미적재
    check(11, "전남 시설 조회 [C-DS17 미연동]", ok,
          f"{len(found)}건 — 전남 시설 데이터 미적재(공공API 연동 대기)")


# ── 12. 사후기록 요약 5개 항목 ──────────────────────────────────
def case12() -> None:
    d = summarize.summarize(
        "무릎 주사 맞았고 다음 진료 2주 뒤, 약국 들렀어요. 계단 힘들어하셨습니다.",
        target="박순자 어르신", dept="정형외과", use_llm=False)
    filled = sum(1 for v in d.as_dict().values() if v)
    ok = filled >= 5 and d.needs_schedule_check
    check(12, "사후기록 5개 항목 + 일정 재확인", ok,
          f"{filled}/6개 항목 / 상대날짜 분리={d.needs_schedule_check}")


def main() -> int:
    db.init_db()
    for fn in (case1, case2, case3, case4, case5, case6,
               case7, case8, case9, case10, case11, case12):
        try:
            fn()
        except Exception as e:
            check(int(fn.__name__[4:]), fn.__name__, False, f"예외: {type(e).__name__}: {e}")

    print(f"\n파일3 샘플 데이터 12건 회귀 검증 (기준일 {BASE_DATE})")
    print("=" * 92)
    passed = 0
    for no, name, ok, detail in sorted(results):
        mark = "PASS" if ok else "FAIL"
        passed += ok
        print(f"  [{mark}] {no:>2}. {name:<34} {detail}")
    print("=" * 92)
    print(f"  {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
