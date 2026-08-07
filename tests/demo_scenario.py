"""시연 리허설 — 파일2 대표 시나리오를 실제 API로 처음부터 끝까지 실행한다.

목적
  1) 리허설: 발표자가 이 출력을 보며 대본대로 진행할 수 있다
  2) 검증: 각 단계가 실제로 동작하는지 PASS/FAIL로 확인 (Day2 '실제 동작 여부')
  3) 시드 고정: 시작할 때 DB를 초기화해 매번 같은 화면이 나오게 한다

실행
    uvicorn donghaenggori.web.api:app --port 8000     # 다른 터미널에서
    python -m tests.demo_scenario

구성 (파일2 3분판)
    시나리오 A 정상 흐름   0:00~2:00  STEP 1~6
    시나리오 B 실패 대응   2:00~2:45  B-1 정보부족 / B-2 대상자미확인 / B-3 긴급
    마무리                2:45~3:00
"""
from __future__ import annotations

import datetime
import sys
import time

BASE = "http://localhost:8000"
PHONE = "010-1234-5678"        # 박순자
PHONE_NEW = "010-0000-0000"    # 미등록
GUARDIAN = "010-9876-5432"     # 박순자의 딸

results: list[tuple[str, bool, str]] = []
_t0 = 0.0


def check(step: str, ok, detail: str = "") -> None:
    results.append((step, bool(ok), detail))


def say(text: str = "") -> None:
    print(text)


def head(title: str, window: str) -> None:
    say()
    say(f"\033[1m{title}\033[0m  {window}")
    say("─" * 74)


def mark(ok: bool) -> str:
    return "\033[32m✔\033[0m" if ok else "\033[31m✘\033[0m"


def api(method: str, path: str, **kw):
    import httpx
    t = time.time()
    r = httpx.request(method, BASE + path, timeout=30, **kw)
    return r, time.time() - t


def main() -> int:
    try:
        import httpx
    except ImportError:
        print("httpx 필요: pip install httpx")
        return 1

    try:
        httpx.get(BASE + "/api/health", timeout=3)
    except Exception:
        print(f"API 서버에 연결할 수 없습니다: {BASE}")
        print("다른 터미널에서 실행하세요:")
        print("  uvicorn donghaenggori.web.api:app --port 8000")
        return 1

    today = datetime.date.today()
    say("\033[1m동행고리 AI — 시연 리허설\033[0m  (파일2 대표 시나리오 3분판)")
    say(f"오늘 {today} · 대상자 박순자 · 시드 초기화 후 시작")
    say("=" * 74)

    api("POST", "/api/reset")

    # 예열 — 외부 API 캐시를 미리 채워 시연 중 지연을 없앤다
    rw, elw = api("POST", "/api/warmup")
    warmed = rw.json() if rw.status_code == 200 else {}
    say(f"   예열 완료 {elw:.1f}s · {sum(1 for v in warmed.get('warmed',{}).values() if v=='ok')}개 캐시 적재")

    # ══════════════════════ 시나리오 A — 정상 흐름 ══════════════════════
    head("시나리오 A — 정상 흐름", "목표 0:00 ~ 2:00")

    # STEP 1~2 : 발화 → 대상자 조회 → 접수카드
    say("\033[1mSTEP 1\033[0m  어르신의 짧고 모호한 전화                      (0:00~0:15)")
    utt = "모레 정형외과 가야겄어. 저번에 무릎 봐준 데."
    say(f'   발화: "{utt}"')
    say("   멘트: 어르신은 앱을 설치하지 않았고, 챗봇과 대화하지도 않습니다.")

    say()
    say("\033[1mSTEP 2~4\033[0m  대상자 조회 → 이력 소환 → 접수카드 생성        (0:15~1:30)")
    r, el = api("POST", "/api/intakes",
                json={"phone": PHONE, "utterance": utt, "channel": "전화"})
    ok = r.status_code == 200
    check("STEP 2~4 접수카드 생성", ok, f"HTTP {r.status_code} · {el:.1f}s")
    if not ok:
        say(f"   {mark(False)} 실패: {r.text[:120]}")
        return _report()

    d = r.json()
    c = d["card"]
    iid = d["intake_id"]
    say(f"   {mark(True)} 대상자   : {c['target']} ({c['phone_masked']})")
    say(f"   {mark(True)} 의도     : {d['intent']}  ({d['intent_source']} {d['intent_confidence']:.2f})")
    say(f"   {mark(True)} 병원 후보: {c['hospital']}  [{c['hospital_status']}]")
    say(f"                근거: {c['reasons'][0] if c['reasons'] else '—'}")
    say(f"   {mark(True)} 방문 예정: {c['date_label']} → {c['date_value']}")
    say(f"   {mark(True)} 동행 수준: {c['need_level']} ({', '.join(c['need_reasons'])})")
    if c["outing_checklist"]:
        for x in c["outing_checklist"]:
            say(f"   {mark(True)} 외출 전  : {x}")
    say(f"   소요 {el:.1f}s")

    check("병원 후보 = 확인됨", c["hospital_status"] == "확인됨", c["hospital_status"])
    check("날짜 해석(모레)", bool(c["date_value"]), str(c["date_value"]))
    check("학습모델 사용", d["intent_source"] == "학습모델", d["intent_source"])

    # STEP 5 : 확정
    say()
    say("\033[1mSTEP 5\033[0m  사회복지사 확인전화 → 확정·배정                 (1:30~1:45)")
    for q in c["confirm_questions"] or ["(확인 질문 없음 — 단골+날짜 확실)"]:
        say(f"   콜백: {q}")
    r2, el2 = api("POST", f"/api/intakes/{iid}/confirm",
                  json={"hospital": c["hospital"], "date": c["date_value"],
                        "level": c["need_level"]})
    ok2 = r2.status_code == 200
    check("STEP 5 확정·배정", ok2, f"HTTP {r2.status_code}")
    say(f"   {mark(ok2)} 확정 저장 · 감사 로그 기록  ({el2:.1f}s)")

    # 권한 분리 확인
    r3, _ = api("POST", f"/api/intakes/{iid}/confirm",
                json={"hospital": "X", "date": "2026-01-01", "level": "단순 안내",
                      "role": "동행매니저"})
    check("RBAC — 동행매니저 확정 거부", r3.status_code == 403, f"HTTP {r3.status_code}")
    say(f"   {mark(r3.status_code == 403)} 동행매니저 권한으로는 확정 불가 (403)")

    # STEP 6 : 사후기록
    say()
    say("\033[1mSTEP 6\033[0m  동행 후 사후기록 자동 초안                      (1:45~2:00)")
    memo = "오늘 무릎 주사 맞았고, 다음 진료는 2주 뒤. 약국 들러서 약 받았어요. 계단 힘들어하셨습니다."
    say(f'   매니저 음성 메모: "{memo}"')
    r4, el4 = api("POST", "/api/post-records",
                  json={"intake_id": iid, "phone": PHONE, "memo": memo,
                        "dept": "정형외과", "target": "박순자 어르신"})
    ok4 = r4.status_code == 200
    check("STEP 6 사후기록 초안", ok4, f"HTTP {r4.status_code}")
    if ok4:
        pr = r4.json()
        for k, v in pr["draft"].items():
            if v:
                say(f"   {mark(True)} {k:<15} {v}")
        check("상대날짜 미확정 처리", pr["needs_schedule_check"], str(pr["needs_schedule_check"]))
        say(f"   {mark(pr['needs_schedule_check'])} '2주 뒤'는 확정하지 않고 일정 재확인 항목으로 분리")
        r5, _ = api("POST", f"/api/post-records/{pr['record_id']}/approve",
                    json={"approved": True})
        check("프로필 업데이트 승인", r5.status_code == 200, f"HTTP {r5.status_code}")
        say(f"   {mark(r5.status_code == 200)} 사회복지사 승인 → 프로필 반영 + 감사 로그")

    # ══════════════════════ 시나리오 B — 실패 대응 ══════════════════════
    head("시나리오 B — 실패·저확신 대응", "목표 2:00 ~ 2:45")

    # B-1 정보 부족
    say("\033[1mB-1\033[0m  이력에 없는 요청                                 (2:00~2:15)")
    r, el = api("POST", "/api/intakes",
                json={"phone": "010-7777-8888", "utterance": "내일 그 큰 병원 좀 가야 쓰겄는디"})
    b1 = r.json()["card"] if r.status_code == 200 else {}
    ok = b1.get("hospital") is None and b1.get("hospital_status") == "확인 필요"
    check("B-1 확정 후보 없음", ok, f"{b1.get('hospital')} [{b1.get('hospital_status')}]")
    say(f"   {mark(ok)} 확정 후보: 없음  [{b1.get('hospital_status')}]")
    refs = b1.get("reference_candidates") or []
    check("B-1 거리 기준 참고 후보", len(refs) > 0, f"{len(refs)}건")
    for h in refs[:2]:
        say(f"   {mark(True)} 참고 후보: {h['name']} ({h['distance_m']:.0f}m) — {h['basis']}")
    say("   멘트: 모를 때는 모른다고 표시합니다. 추측을 답처럼 보여주지 않습니다.")

    # B-2 대상자 미확인
    say()
    say("\033[1mB-2\033[0m  대상자 식별 실패                                 (2:15~2:30)")
    r, _ = api("POST", "/api/intakes",
               json={"phone": PHONE_NEW, "utterance": "병원 좀 가야 해"})
    b2 = r.json()["card"] if r.status_code == 200 else {}
    ok = "신규" in (b2.get("target") or "")
    check("B-2 대상자 미확인 처리", ok, b2.get("target", ""))
    say(f"   {mark(ok)} 대상자: {b2.get('target')}")
    say("   멘트: 전화번호 하나로 사람을 확정하지 않습니다.")

    # B-2' 보호자 대리 전화
    r, _ = api("POST", "/api/intakes",
               json={"phone": GUARDIAN, "utterance": "느그 어매 병원 좀 델꼬 가야 쓰겄는디"})
    b2b = r.json()["card"] if r.status_code == 200 else {}
    cands = b2b.get("target_candidates") or []
    ok = b2b.get("requester") == "대리" and len(cands) == 1
    check("B-2' 대리 전화 → 후보 역조회", ok, f"{b2b.get('requester')} / {[x['name'] for x in cands]}")
    say(f"   {mark(ok)} 보호자 대리: {b2b.get('proxy_relation')} → 후보 {[x['name'] for x in cands]}")

    # B-3 긴급
    say()
    say("\033[1mB-3\033[0m  긴급 신호 감지                                   (2:30~2:45)")
    r, _ = api("POST", "/api/intakes",
               json={"phone": PHONE, "utterance": "가슴이 답답하고 숨이 차"})
    b3 = r.json() if r.status_code == 200 else {}
    ok = b3.get("urgent") and b3.get("card") is None
    check("B-3 긴급 → 카드 미생성", ok, f"urgent={b3.get('urgent')} card={b3.get('card')}")
    say(f"   {mark(ok)} 접수카드 생성 중단 · 사람 상담으로 즉시 연결")
    say(f"   {b3.get('urgent_message','')}")
    say("   멘트: 동행고리 AI는 응급 여부를 판단하지 않습니다.")

    # ══════════════════════ 마무리 ══════════════════════
    head("마무리", "목표 2:45 ~ 3:00")
    r, _ = api("GET", "/api/dashboard")
    counts = r.json()["counts"] if r.status_code == 200 else {}
    say(f"   대시보드: {counts}")
    r, _ = api("GET", "/api/audit")
    n = len(r.json()) if r.status_code == 200 else 0
    check("감사 로그 기록", n > 0, f"{n}건")
    say(f"   {mark(n>0)} 감사 로그 {n}건 — 누가 무엇을 확정·승인했는지 남음")
    say()
    say("   최종 메시지: AI는 사람을 대체하지 않습니다.")
    say("               접수·기억·정리 업무만 줄여, 사회복지사가 더 많은 어르신을 돌보게 합니다.")

    return _report()


def _report() -> int:
    say()
    say("=" * 74)
    passed = sum(1 for _, ok, _ in results if ok)
    for step, ok, detail in results:
        say(f"  [{'PASS' if ok else 'FAIL'}] {step:<34} {detail}")
    say("=" * 74)
    say(f"  {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
