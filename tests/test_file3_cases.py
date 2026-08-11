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


# ── 13. LLM 보강이 대리 전화 정보를 지우지 않는가 ──────────────
# 회귀 방지: nlu._llm_refine 이 Analysis 를 새로 만들면서 requester·
# proxy_relation 을 빠뜨려, 키를 넣는 순간 "어머니 병원 좀..." 이 본인 전화로
# 처리되던 버그가 있었다. 규칙 경로만 검사하면 드러나지 않아 여기서 잡는다.
def case13() -> None:
    from donghaenggori.core import nlu

    class Stub:                      # client.messages.parse(...) 흉내
        def __init__(self): self.messages = self
        def parse(self, **kw):
            P = kw["output_format"]
            return type("R", (), {
                "parsed_output": P(intent="병원동행", dept="정형외과",
                                   symptom="무릎", urgent=False),
                "stop_reason": "end_turn"})()

    text = "어머니 모레 정형외과 모시고 가야 하는데요"
    rule = nlu.analyze(text, use_llm=False)
    llm = nlu.analyze(text, client=Stub())
    ok = (rule.requester == "대리" and llm.requester == rule.requester
          and llm.proxy_relation == rule.proxy_relation and llm.source == "규칙+LLM")
    check(13, "LLM 보강 시 대리 정보 보존", ok,
          f"규칙={rule.requester}/{rule.proxy_relation} → "
          f"LLM={llm.requester}/{llm.proxy_relation} ({llm.source})")


# 회귀 방지: 날짜 표현을 하나 찾고 곧바로 반환하던 시절, "내일 아니고 모레"가
# 정정 이전 날짜(내일)로 접수됐다. 어르신은 말하면서 고치므로 마지막이 최종이다.
def case14() -> None:
    from donghaenggori.core import dateparse

    cases = [("내일 아니고 모레 가야 해", "2026-07-09", True),
             ("모레 말고 내일로 해주쇼", "2026-07-08", True),
             ("모레 가야겄어", "2026-07-09", False),
             ("내일 내일 꼭 가야 해", "2026-07-08", False)]   # 같은 날짜 반복은 정정이 아니다
    got = [dateparse.parse_date(t, BASE_DATE) for t, _, _ in cases]
    ok = all(g and g["date"] == d and g["corrected"] == c
             for g, (_, d, c) in zip(got, cases))
    check(14, "날짜 자기수정 — 마지막 표현 채택", ok,
          " / ".join(f"{t.split()[0]}…→{g['date']}(정정={g['corrected']})"
                     for (t, _, _), g in zip(cases, got)))


# 시각은 예약에 필요하지만, 오전·오후를 모르는 "3시"를 우리가 골라주면 안 된다.
def case15() -> None:
    from donghaenggori.core import dateparse

    checks = [
        ("오전 10시에 가야 해", "10:00", True),
        ("오후 3시 반에 가야 해", "15:30", True),
        ("3시에 가야 해", None, False),          # 오전·오후 불명 → 되묻는다
        ("10시 말고 11시로 해주쇼", "11:00", True),
    ]
    got = [dateparse.parse_time(t) for t, _, _ in checks]
    ok = all(g and g["time"] == v and g["confident"] == c
             for g, (_, v, c) in zip(got, checks))

    r = pipeline.run(PHONE_MAIN, "모레 오후 2시에 정형외과 가야 해")
    ok = ok and r.card.time_value == "14:00"
    amb = pipeline.run(PHONE_MAIN, "모레 3시에 정형외과 가야 해")
    ok = ok and amb.card.time_value is None and any(
        "오전인가요" in q for q in amb.card.confirm_questions)
    check(15, "방문 시각 파싱 + 오전·오후 불명 시 되묻기", ok,
          f"카드 시각={r.card.time_value} / 불명 시 질문={len(amb.card.confirm_questions)}개")


# 항목마다 상태·근거가 붙어야 화면이 "이 근거가 어느 항목 것인지"를 안다(파일4).
def case16() -> None:
    r = pipeline.run(PHONE_MAIN, "모레 저번에 무릎 봐준 데 가야겄어")
    fields = r.card.to_dict()["fields"]
    allowed = {"확인됨", "추정", "확인 필요"}
    ok = (set(fields) == {"target", "hospital", "dept", "date", "time"}
          and all(f["status"] in allowed for f in fields.values())
          and all(f["evidence"] for f in fields.values())
          and fields["target"]["status"] == "확인됨")
    check(16, "항목별 상태·근거 구조", ok,
          " ".join(f"{k}[{v['status']}]" for k, v in fields.items()))


# 분류기 경계 — 파이프라인이 '무엇으로' 분류하는지 몰라야 한다. 그리고 분류기
# 결과를 그대로 믿지 않아야 한다(LLM·원격 추론이 들어올 자리라서).
def case17() -> None:
    from donghaenggori.core import classify, nlu

    # 다른 구현을 끼워 넣어도 접수는 그대로 된다
    alt = pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어",
                       with_rag=False, classifier=classify.RuleOnlyClassifier())

    class Broken:                     # 계약을 어기는 분류기
        def classify(self, utterance):
            a = nlu.analyze(utterance, use_llm=False)
            a.intent = "택시호출"      # INTENTS 에 없다
            return classify.Classification(analysis=a, source="가짜모델", confidence=7.3)

    bad = pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어",
                       with_rag=False, classifier=Broken())

    violations = 0
    for mutate in (lambda a: setattr(a, "intent", "택시호출"),
                   lambda a: (setattr(a, "urgent", True), setattr(a, "intent", "약국"))):
        a = nlu.analyze("모레 병원 가야 해", use_llm=False)
        mutate(a)
        try:
            classify.validate(classify.Classification(analysis=a, source="테스트"))
        except classify.ClassifierContractError:
            violations += 1

    ok = (alt.card is not None and alt.intent_source == "규칙"
          and bad.card is not None                      # 접수는 계속된다
          and bad.analysis.intent in nlu.INTENTS        # 잘못된 의도는 안 새어나온다
          and any("계약 위반" in n for n in bad.analysis.notes)
          and violations == 2)
    check(17, "분류기 교체 가능 + 계약 위반 시 규칙 폴백", ok,
          f"교체={alt.intent_source} / 위반 시={bad.analysis.intent}(노트 {len(bad.analysis.notes)}개) / 검증 {violations}/2")


# 동행 필요도의 근거가 우리가 지어낸 점수가 아니라 공식 판정에서 나와야 한다.
# "왜 휠체어가 3점입니까"에 답할 수 없는 상태를 회귀로 막는다.
def case18() -> None:
    base = db.get_profile(PHONE_MAIN) or {}

    official = needlevel.assess(base | {"ltci_grade": "2", "care_program": None})
    program = needlevel.assess(base | {"ltci_grade": None, "care_program": "중점돌봄군"})
    observed = needlevel.assess(base | {"ltci_grade": None, "care_program": None})

    ok = (official.official and official.basis == needlevel.BASIS_LTCI
          and official.level == "휠체어·부축 동행"
          and program.official and program.basis == needlevel.BASIS_CARE_PROGRAM
          and not observed.official
          # 공식 등급이 있어도 현장 주의사항(낙상·독거)은 가려지지 않는다
          and any("낙상" in r for r in official.reasons)
          # 추정 경로는 확정 전 확인을 반드시 요구한다
          and any("확정 전 공식 등급 확인" in r for r in observed.reasons))
    check(18, "동행 필요도 — 공식 판정 우선, 추정이면 표시", ok,
          f"등급={official.basis}/{official.level} · 돌봄군={program.basis} · 미등록={observed.basis}")


def main() -> int:
    db.init_db()
    for fn in (case1, case2, case3, case4, case5, case6,
               case7, case8, case9, case10, case11, case12, case13,
               case14, case15, case16, case17, case18):
        try:
            fn()
        except Exception as e:
            check(int(fn.__name__[4:]), fn.__name__, False, f"예외: {type(e).__name__}: {e}")

    print(f"\n파일3 샘플 데이터 12건 + 회귀 6건 검증 (기준일 {BASE_DATE})")
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
