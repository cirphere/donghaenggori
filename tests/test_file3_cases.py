"""제출 문서(파일3 샘플데이터)의 테스트 케이스 12건 회귀 검증.

심사 당일 "이 케이스 돌려보세요"에 그대로 답할 수 있어야 한다.
문서의 접수일 기준은 2026-07-07(화)이므로, 날짜 해석은 그 날짜로 고정해 검증한다.

실행:  .venv/bin/python -m tests.test_file3_cases
"""
from __future__ import annotations

import datetime
import sys

from donghaenggori.core import dateparse, db, hospital, needlevel, pipeline
from donghaenggori.services import rag, summarize

BASE_DATE = datetime.date(2026, 7, 7)      # 문서 기준 접수일 (화)
PHONE_MAIN = "010-1234-5678"               # 박순자 — 정형외과 단골 2회
PHONE_NEW = "010-0000-0000"                # 미등록 번호

results: list[tuple[int, str, bool, str]] = []


def check(no: int, name: str, passed, detail: str) -> None:
    results.append((no, name, bool(passed), detail))


# ── 1. 단골 → 추정 + 날짜 해석 + 확인 질문 ──────────────────────
# 단골도 '추정' 이다. 어르신이 이번 통화에서 병원을 말한 적이 없기 때문이다
# (hospital 모듈 설명 참조). 후보와 근거는 그대로 나오고, 확정만 사람이 한다.
def case1() -> None:
    r = pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어. 저번에 무릎 봐준 데")
    c = r.card
    d = dateparse.parse_date("모레", today=BASE_DATE)
    ok = (c.dept == "정형외과" and c.hospital_status == "추정" and c.hospital is not None
          and d["date"] == "2026-07-09" and len(r.analysis.date or {}) > 0)
    check(1, "단골 → 추정 + 날짜 해석", ok,
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
    r = pipeline.run("010-9876-5432", "우리 어매 병원 좀 델꼬 가야 쓰겄는디")  # 박순자의 딸
    c = r.card
    ok = (c.requester == "대리" and c.proxy_relation == "어머니"
          and len(c.target_candidates) == 1
          and c.target_candidates[0]["name"] == "박순자"
          and any("대리 요청" in f for f in c.flags))
    check(7, "대리 전화 → 대상자 후보 역조회", ok,
          f"{c.requester}({c.proxy_relation}) → 후보 {[x['name'] for x in c.target_candidates]} / {c.flags[0]}")


# ── 7-b. 대리 요청에 발신자 프로필이 새지 않는가 ────────────────
# 회귀 방지: "우리 어매 병원 좀" 을 **등록된 대상자가 자기 번호로** 걸면,
# 대상자 칸에는 '미확인(어머니 대리 요청)' 이라 써 놓고 병원·필요도는 발신자
# 것으로 채웠다. 딸의 단골이 어머니의 병원으로 '확인됨' 이 됐다.
# 보호자 번호로 걸 때는 그 번호가 대상자로 등록돼 있지 않아 드러나지 않던 구멍이다.
def case7b() -> None:
    r = pipeline.run(PHONE_MAIN, "우리 어매 병원 좀 델꼬 가야 쓰겄는디")
    c = r.card
    샘 = (c.hospital is not None or c.need_level != "확인 필요")
    본인 = pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어. 저번에 무릎 봐준 데").card
    # 본인 요청 쪽은 대조군이다 — 같은 번호라도 본인이 물으면 이력이 후보로 붙는다.
    # 그 상태는 '확인됨' 이 아니라 '추정' 이다(직접 말한 병원이 아니므로).
    check(15, "대리 요청에 발신자 이력이 안 붙는다",
          not 샘 and 본인.hospital is not None and 본인.hospital_status == "추정",
          f"대리: 병원={c.hospital}/{c.need_level} · 본인: {본인.hospital}[{본인.hospital_status}]")


# ── 7-c. 대리 판별이 낱말 경계를 지키는가 ───────────────────────
# 부분문자열로만 보던 시절 "어머니날에 병원 가야" 가 어머니 대리 요청이 됐다.
# 대리로 잡히면 발신자 프로필을 버리므로(case7b), 오탐의 대가가 크다.
def case7c() -> None:
    from donghaenggori.core import nlu

    대리 = ["우리 어매 병원 좀 델꼬 가야", "어머니 모시고 병원", "아버지 병원 좀",
           "집사람 병원 데려다", "딸이 대신 전화했어요", "어르신 모시고 병원 가야",
           "우리 어른 병원 좀"]
    본인 = ["무릎이 아파서 병원 가야", "모레 정형외과 가야겄어",
           "어머니날에 병원 가야", "엄마손 식당 앞 병원", "할머니회 모임 갔다가",
           "어르신 병원 동행 신청합니다"]     # '어르신' 단독은 대상자 호칭이다
    틀림 = ([t for t in 대리 if nlu.detect_proxy(t)[0] != "대리"]
           + [t for t in 본인 if nlu.detect_proxy(t)[0] != "본인"])
    check(16, "대리 판별 — 낱말 경계", not 틀림,
          f"{len(대리) + len(본인)}건 중 틀림 {len(틀림)}: {틀림[:2]}")


# ── 7-d. 긴급 발화에서도 대리를 판별하는가 ──────────────────────
# 규칙이 긴급을 만나면 곧바로 반환해서 detect_proxy 가 아예 돌지 않았다.
# "우리 어매가 쓰러졌어" 가 발신자 본인 이름으로 응급 기록에 남았다 —
# 그 기록을 보고 사람이 엉뚱한 어르신을 찾아간다.
def case7d() -> None:
    from donghaenggori.core import nlu

    a = nlu.analyze("우리 어매가 쓰러졌어 숨을 못 쉬어", use_llm=False)
    r = pipeline.run(PHONE_MAIN, "우리 어매가 쓰러졌어 숨을 못 쉬어")
    본인긴급 = pipeline.run(PHONE_MAIN, "가슴이 답답하고 숨이 차")
    check(17, "긴급 + 대리 → 발신자를 대상자로 쓰지 않는다",
          a.urgent and a.requester == "대리" and r.profile is None
          and 본인긴급.profile is not None,
          f"대리={a.requester} 프로필={r.profile} · 본인긴급 프로필={bool(본인긴급.profile)}")


# ── 7-e. 병원명도 정정을 따르는가 ───────────────────────────────
# 날짜는 "내일 아니고 모레" 를 처리하는데 병원은 첫 번째를 잡고 있었다.
def case7e() -> None:
    from donghaenggori.core import nlu

    쌍 = [("송정병원 말고 목포한국병원으로 가야", "목포한국병원"),
         ("송정병원 아니고 순천의료원", "순천의료원"),
         ("목포 병원 말고 송정병원", "송정병원"),
         ("내일 아니고 모레 송정병원", "송정병원"),        # 날짜 정정은 병원과 무관
         ("전남대학교 병원 갔다가 목포한국병원도", "전남대학교병원")]  # 정정이 아니면 첫 번째
    틀림 = [t for t, want in 쌍 if nlu.detect_hospital(t) != want]
    check(18, "병원명 정정 — 나중 것 채택", not 틀림, f"틀림 {len(틀림)}: {틀림[:2]}")


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


# ── 11. 전남 대상자 → 시설 조회 (C-DS04 노인복지관) ─────────────
def case11() -> None:
    """관내 시설이 있으면 관내로, 없으면 '같은 시도'라고 밝혀야 한다.

    신안군은 표에 노인복지관이 없다. 이때 다른 시군 시설을 아무 표시 없이
    1순위로 올리면 섬 주민에게 100km 떨어진 곳을 권하는 셈이 된다.
    """
    goheung = rag.search(region="전남 고흥군 ○○면", limit=3)
    sinan = rag.search(region="전남 신안군 ○○면(섬)", limit=3)
    ok = (len(goheung) > 0 and goheung[0]["region_match"] == "관내"
          and goheung[0]["source"] == "C-DS04"
          and len(sinan) > 0 and all(f["region_match"] != "관내" for f in sinan))
    check(11, "전남 노인복지관 조회 + 관내/타지역 구분", ok,
          f"고흥 {len(goheung)}건[{goheung[0]['region_match'] if goheung else '—'}] / "
          f"신안 {len(sinan)}건[{sinan[0]['region_match'] if sinan else '—'}]")


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
             for g, (_, d, c) in zip(got, cases, strict=True))
    check(14, "날짜 자기수정 — 마지막 표현 채택", ok,
          " / ".join(f"{t.split()[0]}…→{g['date']}(정정={g['corrected']})"
                     for (t, _, _), g in zip(cases, got, strict=True)))


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
             for g, (_, v, c) in zip(got, checks, strict=True))

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
    # mobility_need 는 발화 기반 이동지원 필요 여부다(core/mobility.py). 프로필의
    # 거동 상태(card.mobility)와 다른 값이라 항목으로 따로 선다.
    ok = (set(fields) == {"target", "hospital", "dept", "date", "time", "mobility_need"}
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


# 긴급이 목록 맨 위에 오고, 처리하면 내려가야 한다. 최신순만 쓰면 긴급이
# 이후 접수에 밀려 묻히고, 처리 표시가 없으면 반대로 영원히 맨 위를 덮는다.
def case19() -> None:
    pipeline.run(PHONE_MAIN, "가슴이 답답하고 숨이 차")
    class _Stub:
        target, raw_utterance, intent = "박순자", "가슴이 답답하고 숨이 차", "긴급"
        hospital = hospital_status = dept = None
        date_value = date_label = need_level = None
    uid = db.save_intake(_Stub(), PHONE_MAIN, "전화", status="긴급")
    # 긴급 뒤에 평범한 접수를 넣어도 긴급이 위에 있어야 한다
    pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어")
    db.save_intake(pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어").card, PHONE_MAIN, "전화")

    top = db.list_intakes(limit=1)[0]
    on_top = top["status"] == "긴급"

    changed = db.resolve_urgent(uid, "김○○ 사회복지사", "사회복지사", "통화 완료")
    again = db.resolve_urgent(uid, "김○○ 사회복지사", "사회복지사", "재요청")
    row = db.get_intake(uid)
    ids_above = [x["id"] for x in db.list_intakes(limit=50)]
    moved_down = (ids_above.index(uid) > 0 if uid in ids_above else True)

    ok = (on_top and changed and not again
          and row["status"] == "긴급 처리됨" and moved_down)
    check(19, "긴급 우선 정렬 + 처리 완료 표시", ok,
          f"맨위={top['status']} / 처리={changed} 재요청={again} → {row['status']}")


# "마지막 표현이 최종"은 **말을 고쳤을 때만** 맞다. 선택지·범위·부정까지
# 확정해 버리면 헛걸음이 난다 — 어르신은 "화요일이나 수요일"처럼 자주 말한다.
def case20() -> None:
    cases = [
        # (발화, 종류, 값, 확신)
        ("내일 아니고 모레 가야 해",      "date", "2026-07-09", True),   # 정정
        ("모레 가야겄어",               "date", "2026-07-09", True),   # 단일
        ("화요일이나 수요일에 가야지",      "date", None,         False),  # 선택지
        ("내일부터 모레까지 시간 돼",      "date", None,         False),  # 범위
        ("10시 말고 11시로 해주쇼",      "time", "11:00",      True),   # 정정
        ("10시나 11시쯤 가면 돼",       "time", None,         False),  # 선택지
        ("10시부터 11시 사이에",        "time", None,         False),  # 범위
        ("10시는 아니에요",            "time", None,         False),  # 부정
        ("3시에 가야 해",             "time", None,         False),  # 오전·오후 불명
    ]
    bad = []
    for text, kind, want, conf in cases:
        r = (dateparse.parse_date(text, BASE_DATE) if kind == "date"
             else dateparse.parse_time(text))
        got = (r or {}).get("date" if kind == "date" else "time")
        if got != want or (r or {}).get("confident", False) != conf:
            bad.append(text)

    # 확인 질문이 사유에 맞아야 한다 — "10시나 11시"에 대고 오전·오후를 물으면 안 된다
    q = pipeline.run(PHONE_MAIN, "모레 10시나 11시쯤 정형외과").card.confirm_questions
    right_q = any("어느 쪽" in x for x in q) and not any("오전인가요" in x for x in q)

    check(20, "선택지·범위·부정은 확정하지 않는다", not bad and right_q,
          (f"실패 {bad}" if bad else "9/9") + f" · 질문={'사유별' if right_q else '어긋남'}")


# 통신망은 국가번호를 붙여 준다. 이걸 처리 못 하면 실전화가 오는 순간
# 모든 통화가 '신규 대상자(미등록 번호)'가 된다.
def case21() -> None:
    forms = ["010-1234-5678", "01012345678", "+821012345678", "821012345678"]
    names = [(db.get_profile(f) or {}).get("name") for f in forms]
    ok = all(n == names[0] and n for n in names)
    check(21, "발신번호 국가번호(+82) 정규화", ok,
          " / ".join(f"{f}→{n or '못 찾음'}" for f, n in zip(forms, names, strict=True)))


# 어르신이 병원 이름을 직접 대면 그것이 과거 이력보다 우선이다.
# 실통화에서 "내일 송정병원으로 가야 될 것 같아" 를 받고도 이력의 다른 병원을
# '확인됨' 으로 내놓은 적이 있다 — 그대로 확정하면 엉뚱한 곳으로 배차된다.
def case22() -> None:
    from donghaenggori.core import nlu

    뽑기 = [("허리 아파서 내일 송정병원으로 10시에 가야 될 것 같아", "송정병원"),
           ("전남대학교 병원 가야 해", "전남대학교병원"),      # 띄어 쓴 표기
           ("고흥 보건소 좀 데려다 주쇼", "고흥보건소"),
           ("저번에 갔던 병원으로 가주쇼", None),              # 이름이 아니다
           ("내일 그 큰 병원 좀", None),
           ("내과 병원 가야 해", None),                       # 진료과를 말한 것
           # 날짜만 말하고 병원 이름은 안 댄 경우. '내일병원' 이라는 없는 병원을
           # 만들어 '확인됨' 까지 붙였다 — 어르신이 가장 흔하게 하는 말이다.
           ("내일 병원 가야 해요", None),
           ("오늘 병원 갔다왔어", None),
           ("다음주 병원 예약했는디", None),
           ("아까 의원 갔다가", None),
           # 가족 호칭. 대리 접수에서 가장 흔한 말투인데 '어매병원' 이라는 없는
           # 병원을 만들어 '확인됨' 으로 띄웠다. 대리 판별은 '어매' 를 어머니로
           # 알고 있었는데 병원명 쪽만 몰랐다 — 목록을 한 곳에서 가져오게 했다.
           ("우리 어매 병원 좀 델꼬 가야 쓰겄는디", None),
           ("우리 엄마 병원 모시고 가야 해", None),
           ("할머니 병원 데리고 가야 돼", None),
           ("우리 아들 병원 갔다", None),
           # 호칭이 있어도 진짜 병원 이름을 대면 그건 잡아야 한다
           ("우리 어매 송정병원 델꼬 가야 쓰겄는디", "송정병원"),
           # **띄어 쓴 '병원' 은 대개 상호가 아니다.** 차단 목록으로는 감당이
           # 안 됐다 — 흔한 발화 24개 중 20개가 없는 병원을 만들어냈다.
           # 한국어 어미를 다 셀 수는 없어서, 붙여 쓴 것만 상호로 본다.
           ("몸이 안 좋아서 병원 좀 가야 쓰겄어", None),
           ("주사 맞으러 병원", None),
           ("택시 타고 병원", None),
           ("무릎 때문에 병원", None),
           ("목포 병원 좀 데려다 주쇼", None),      # 목포에 있는 아무 병원
           ("걸어서 보건소", None),                # 구체적 꼬리라도 앞이 용언이면
           # 붙여 쓴 상호와 띄어 쓰는 것이 자연스러운 표기는 그대로 잡는다
           ("녹동현대병원 가야 해", "녹동현대병원"),
           ("순천의료원 가야 하는디", "순천의료원"),
           ("하의면 보건지소 가야", "하의면보건지소"),
           ("신안군 보건소 가야", "신안군보건소")]
    나쁨 = [t for t, want in 뽑기 if nlu.detect_hospital(t) != want]

    r = pipeline.run(PHONE_MAIN, "허리 아파서 내일 송정병원으로 10시에 가야 될 것 같아")
    f = r.card.to_dict()["fields"]["hospital"]
    말한대로 = f["value"] == "송정병원" and f["status"] == "확인됨"
    이력도알림 = any("과거 이력과 다름" in e for e in f["evidence"])

    # 이름을 안 댔으면 예전처럼 이력을 쓴다
    r2 = pipeline.run(PHONE_MAIN, "모레 정형외과 가야겄어. 저번에 무릎 봐준 데")
    이력유지 = r2.card.hospital == "○○정형외과의원"

    check(22, "발화의 병원명이 이력보다 우선", not 나쁨 and 말한대로 and 이력도알림 and 이력유지,
          (f"추출 실패 {나쁨}" if 나쁨 else "추출 6/6") +
          f" · 말한대로={말한대로} 이력알림={이력도알림} 이력유지={이력유지}")


def case23() -> None:
    """'내일모레' 는 하루다. 그리고 고친 말과 고르라는 말을 가른다.

    실통화: "나 내일 모레 한 오후쯤에 한 3시 넘어가지고 정형외과 갈라 하는데"

    '내일' 과 '모레' 가 각각 후보로 잡혀 '복수 표현' 으로 걸렸다. 날짜가 비면
    확정 게이트가 막히고, 어르신은 분명하게 하루를 말했는데 확인 전화를 한 통
    더 받는다.

    **고친 말과 고르라는 말은 다르다.** "내일 아니고 모레" 는 앞을 물린
    것이라 모레로 확정해도 되지만, "내일 아니면 모레" 는 둘 중 하나를
    고르라는 말이라 우리가 고르면 안 된다 — 절반의 확률로 어르신이 엉뚱한
    날 병원 앞에 선다.
    """
    하루 = [("내일모레 병원 가야 해", 2), ("내일 모레 병원 가야 해", 2),
            ("낼모레 가야겄어", 2), ("낼 모레 가야겄어", 2),
            ("내일 가야 해", 1), ("모레 가야 해", 2)]
    for 말, 며칠 in 하루:
        want = (BASE_DATE + datetime.timedelta(days=며칠)).isoformat()
        got = dateparse.parse_date(말, today=BASE_DATE)
        check(23, f"'{말[:12]}'", got and got["date"] == want,
              f"{got['date'] if got else None} (기대 {want})")

    정정 = ["내일 아니고 모레 가야 해", "내일 말고 모레", "내일 아니라 모레요",
            "내일은 안 되고 모레로 해줘", "내일은 못 가고 모레 가야겄어"]
    want = (BASE_DATE + datetime.timedelta(days=2)).isoformat()
    for 말 in 정정:
        got = dateparse.parse_date(말, today=BASE_DATE)
        check(23, f"정정 '{말[:12]}'", got and got["date"] == want,
              f"{got['date'] if got else None}")

    # 고르라는 말은 비워 둔다 — 확인 질문으로 넘어간다.
    for 말 in ["내일 아니면 모레", "내일이나 글피 중에", "내일이나 모레쯤",
               "내일부터 글피까지"]:
        got = dateparse.parse_date(말, today=BASE_DATE)
        check(23, f"선택 '{말[:12]}'", got is not None and got["date"] is None,
              f"{got['date'] if got else None} — 확정하면 안 된다")


def case24() -> None:
    """요일이 날짜를 확인해 주면 하나로 본다. 그리고 근거는 그 항목의 말로 적는다.

    "9월 5일 토요일" 처럼 날짜와 요일을 함께 말하는 것이 오히려 흔한데,
    요일 규칙이 '다가오는 토요일' 을 따로 계산해 값이 둘이 되고 '복수 표현'
    으로 걸렸다. 어르신은 분명하게 하루를 말했고 두 표현이 서로를 확인해
    주는데도 확인 전화가 한 통 더 나갔다.

    **어긋나면 그대로 되묻는다.** 2026-09-05 는 토요일이므로 "9월 5일
    금요일" 은 둘 중 어느 쪽이 맞는지 우리가 고를 일이 아니다.
    """
    fri = "2026-09-05"                       # 토요일
    for 말, 기대 in (("9월 5일 토요일에 가야 해", fri),
                     ("8월 22일 토요일", "2026-08-22"),
                     ("9월 5일에 가야 해", fri)):
        got = dateparse.parse_date(말, today=BASE_DATE)
        check(24, f"'{말[:14]}'", got and got["date"] == 기대,
              f"{got['date'] if got else None} (기대 {기대})")

    got = dateparse.parse_date("9월 5일 금요일에 가야 해", today=BASE_DATE)
    check(24, "어긋나면 되묻는다", got is not None and got["date"] is None,
          f"{got['date'] if got else None} — 확정하면 안 된다")

    # 근거 문구는 그 항목의 말로 적는다 — 날짜 칸에 '오전·오후' 가 붙었다.
    r = pipeline.run("010-1234-5678", "9월 5일 금요일에 병원 가야 해", channel="전화")
    ev = r.card.to_dict()["fields"]["date"]["evidence"]
    check(24, "날짜 근거에 오전·오후가 없다",
          not any("오전·오후" in e for e in ev), str(ev))
    check(24, "무엇을 물어야 하는지 적힌다",
          any("여러 날짜" in e for e in ev), str(ev))


def main() -> int:
    db.init_db()
    for fn in (case1, case2, case3, case4, case5, case6,
               case7, case7b, case7c, case7d, case7e, case8, case9, case10, case11, case12, case13,
               case14, case15, case16, case17, case18, case19, case20, case21, case22, case23, case24):
        try:
            fn()
        except Exception as e:
            check(int(fn.__name__[4:]), fn.__name__, False, f"예외: {type(e).__name__}: {e}")

    print(f"\n파일3 샘플 데이터 12건 + 회귀 10건 검증 (기준일 {BASE_DATE})")
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
