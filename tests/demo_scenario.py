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

import argparse
import datetime
import os
import sys
import time

BASE = "http://localhost:8000"
PHONE = "010-1234-5678"        # 박순자 — 정형외과 2회 → 확인됨
PHONE_NEW = "010-0000-0000"    # 미등록
GUARDIAN = "010-9876-5432"     # 박순자의 딸
PHONE_FLYWHEEL = "010-2222-3333"   # 김수남 — 정형외과 1회 → 추정 (플라이휠 시연용)
FULL = False                   # True면 본선 Day2 8분판

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


def win(three: str, eight: str) -> str:
    """모드별 시간 창. 3분판은 제출 문서 기준, 8분판은 본선 Day2 Demo 기준."""
    return eight if FULL else three


def api(method: str, path: str, **kw):
    import httpx
    t = time.time()
    r = httpx.request(method, BASE + path, timeout=30, **kw)
    return r, time.time() - t


def main(full: bool = False) -> int:
    global FULL
    FULL = full
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

    # 계정은 미리 만들어 둬야 한다: python -m donghaenggori.services.create_user
    sw_email, sw_pw = os.environ.get("DEMO_SW_EMAIL"), os.environ.get("DEMO_SW_PASSWORD")
    mgr_email, mgr_pw = os.environ.get("DEMO_MGR_EMAIL"), os.environ.get("DEMO_MGR_PASSWORD")
    if not (sw_email and sw_pw and mgr_email and mgr_pw):
        print("DEMO_SW_EMAIL/DEMO_SW_PASSWORD, DEMO_MGR_EMAIL/DEMO_MGR_PASSWORD 환경변수가 필요합니다.")
        print("  python -m donghaenggori.services.create_user 로 사회복지사·동행매니저 계정을 먼저 만드세요.")
        return 1

    today = datetime.date.today()
    span = "본선 Day2 Demo 8분판" if full else "파일2 대표 시나리오 3분판"
    say(f"\033[1m동행고리 AI — 시연 리허설\033[0m  ({span})")
    say(f"오늘 {today} · 대상자 박순자 · 시드 초기화 후 시작")
    say("=" * 74)

    api("POST", "/api/reset")

    # 로그인 — /api/reset이 세션을 지우므로 반드시 리셋 다음에 한다. confirm/verify/
    # approve/audit는 이제 인증 없이 안 된다.
    r, _ = api("POST", "/api/auth/login", json={"email": sw_email, "password": sw_pw})
    if r.status_code != 200:
        print(f"사회복지사 로그인 실패: HTTP {r.status_code} {r.text[:120]}")
        return 1
    SW_AUTH = {"Authorization": f"Bearer {r.json()['token']}"}
    r, _ = api("POST", "/api/auth/login", json={"email": mgr_email, "password": mgr_pw})
    if r.status_code != 200:
        print(f"동행매니저 로그인 실패: HTTP {r.status_code} {r.text[:120]}")
        return 1
    MGR_AUTH = {"Authorization": f"Bearer {r.json()['token']}"}

    # 예열 — 외부 API 캐시를 미리 채워 시연 중 지연을 없앤다
    rw, elw = api("POST", "/api/warmup", headers=SW_AUTH)
    warmed = rw.json() if rw.status_code == 200 else {}
    say(f"   예열 완료 {elw:.1f}s · {sum(1 for v in warmed.get('warmed',{}).values() if v=='ok')}개 캐시 적재")

    # ══════════════════════ 시나리오 A — 정상 흐름 ══════════════════════
    head("시나리오 A — 정상 흐름", "목표 " + win("0:00 ~ 2:00", "0:00 ~ 5:20"))

    # STEP 1~2 : 발화 → 대상자 조회 → 접수카드
    say("\033[1mSTEP 1\033[0m  어르신의 짧고 모호한 전화                      (" + win("0:00~0:15","0:00~0:30") + ")")
    utt = "모레 정형외과 가야겄어. 저번에 무릎 봐준 데."
    say(f'   발화: "{utt}"')
    say("   멘트: 어르신은 앱을 설치하지 않았고, 챗봇과 대화하지도 않습니다.")

    say()
    say("\033[1mSTEP 2~4\033[0m  대상자 조회 → 이력 소환 → 접수카드 생성        (" + win("0:15~1:30","0:30~3:10") + ")")
    r, el = api("POST", "/api/intakes",
                json={"phone": PHONE, "utterance": utt, "channel": "전화"}, headers=SW_AUTH)
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
    say("\033[1mSTEP 5\033[0m  사회복지사 확인전화 → 확정·배정                 (" + win("1:30~1:45","3:10~3:50") + ")")
    for q in c["confirm_questions"] or ["(확인 질문 없음 — 단골+날짜 확실)"]:
        say(f"   콜백: {q}")
    r2, el2 = api("POST", f"/api/intakes/{iid}/confirm",
                  json={"hospital": c["hospital"], "date": c["date_value"],
                        "level": c["need_level"]}, headers=SW_AUTH)
    ok2 = r2.status_code == 200
    check("STEP 5 확정·배정", ok2, f"HTTP {r2.status_code}")
    say(f"   {mark(ok2)} 확정 저장 · 감사 로그 기록  ({el2:.1f}s)")

    # 권한 분리 확인 — role은 이제 토큰이 정한다, 본문으로는 못 바꾼다
    r3, _ = api("POST", f"/api/intakes/{iid}/confirm",
                json={"hospital": "X", "date": "2026-01-01", "level": "단순 안내"},
                headers=MGR_AUTH)
    check("RBAC — 동행매니저 확정 거부", r3.status_code == 403, f"HTTP {r3.status_code}")
    say(f"   {mark(r3.status_code == 403)} 동행매니저 권한으로는 확정 불가 (403)")

    # STEP 6 : 사후기록
    say()
    say("\033[1mSTEP 6\033[0m  동행 후 사후기록 자동 초안                      (" + win("1:45~2:00","3:50~4:40") + ")")
    memo = "오늘 무릎 주사 맞았고, 다음 진료는 2주 뒤. 약국 들러서 약 받았어요. 계단 힘들어하셨습니다."
    say(f'   매니저 음성 메모: "{memo}"')
    r4, el4 = api("POST", "/api/post-records",
                  json={"intake_id": iid, "phone": PHONE, "memo": memo,
                        "dept": "정형외과", "target": "박순자 어르신"}, headers=SW_AUTH)
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
                    json={"approved": True}, headers=SW_AUTH)
        check("프로필 업데이트 승인", r5.status_code == 200, f"HTTP {r5.status_code}")
        say(f"   {mark(r5.status_code == 200)} 사회복지사 승인 → 프로필 반영 + 감사 로그")

    # ═══════════════ STEP 7 — 플라이휠 (8분판 전용) ═══════════════
    if full:
        say()
        say("\033[1mSTEP 7\033[0m  다음 접수에서 달라지는 것 — 플라이휠           (4:40~5:20)")
        u = "허리가 아파서 정형외과 가야 하는디"
        r, _ = api("POST", "/api/intakes", json={"phone": PHONE_FLYWHEEL, "utterance": u},
                   headers=SW_AUTH)
        c1 = r.json()["card"]
        say(f'   1차 접수: "{u}"')
        say(f"   {mark(True)} {c1['hospital']}  [{c1['hospital_status']}]  ← 1회 방문이라 '추정'")

        today = datetime.date.today().isoformat()
        api("POST", "/api/flywheel", json={"phone": PHONE_FLYWHEEL, "date": today,
                                           "hospital": c1["hospital"], "dept": "정형외과"},
            headers=SW_AUTH)
        say(f"   {mark(True)} 동행 완료 → 이력에 누적")

        r, _ = api("POST", "/api/intakes", json={"phone": PHONE_FLYWHEEL, "utterance": u},
                   headers=SW_AUTH)
        c2 = r.json()["card"]
        rose = c1["hospital_status"] == "추정" and c2["hospital_status"] == "확인됨"
        check("STEP 7 플라이휠 — 상태 상승", rose,
              f"{c1['hospital_status']} → {c2['hospital_status']}")
        say(f"   {mark(rose)} 2차 접수: {c2['hospital']}  [{c2['hospital_status']}]  ← 2회가 되어 '확인됨'")
        say(f"                근거: {c2['reasons'][0]}")
        say("   멘트: 쓸수록 확인 질문이 줄어듭니다. 기록이 다음 접수의 재료가 됩니다.")

    # ══════════════════════ 시나리오 B — 실패 대응 ══════════════════════
    head("시나리오 B — 실패·저확신 대응", "목표 " + win("2:00 ~ 2:45","5:20 ~ 7:00"))

    # B-1 정보 부족
    say("\033[1mB-1\033[0m  이력에 없는 요청                                 (" + win("2:00~2:15","5:20~5:55") + ")")
    r, el = api("POST", "/api/intakes",
                json={"phone": "010-7777-8888", "utterance": "내일 그 큰 병원 좀 가야 쓰겄는디"},
                headers=SW_AUTH)
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
    say("\033[1mB-2\033[0m  대상자 식별 실패                                 (" + win("2:15~2:30","5:55~6:30") + ")")
    r, _ = api("POST", "/api/intakes",
               json={"phone": PHONE_NEW, "utterance": "병원 좀 가야 해"}, headers=SW_AUTH)
    b2 = r.json()["card"] if r.status_code == 200 else {}
    ok = "신규" in (b2.get("target") or "")
    check("B-2 대상자 미확인 처리", ok, b2.get("target", ""))
    say(f"   {mark(ok)} 대상자: {b2.get('target')}")
    say("   멘트: 전화번호 하나로 사람을 확정하지 않습니다.")

    # B-2' 보호자 대리 전화
    r, _ = api("POST", "/api/intakes",
               json={"phone": GUARDIAN, "utterance": "우리 어매 병원 좀 델꼬 가야 쓰겄는디"},
               headers=SW_AUTH)
    b2b = r.json()["card"] if r.status_code == 200 else {}
    cands = b2b.get("target_candidates") or []
    ok = b2b.get("requester") == "대리" and len(cands) == 1
    check("B-2' 대리 전화 → 후보 역조회", ok, f"{b2b.get('requester')} / {[x['name'] for x in cands]}")
    say(f"   {mark(ok)} 보호자 대리: {b2b.get('proxy_relation')} → 후보 {[x['name'] for x in cands]}")

    # B-3 긴급
    say()
    say("\033[1mB-3\033[0m  긴급 신호 감지                                   (" + win("2:30~2:45","6:30~7:00") + ")")
    r, _ = api("POST", "/api/intakes",
               json={"phone": PHONE, "utterance": "가슴이 답답하고 숨이 차"}, headers=SW_AUTH)
    b3 = r.json() if r.status_code == 200 else {}
    ok = b3.get("urgent") and b3.get("card") is None
    check("B-3 긴급 → 카드 미생성", ok, f"urgent={b3.get('urgent')} card={b3.get('card')}")
    say(f"   {mark(ok)} 접수카드 생성 중단 · 사람 상담으로 즉시 연결")
    say(f"   {b3.get('urgent_message','')}")
    say("   멘트: 동행고리 AI는 응급 여부를 판단하지 않습니다.")

    # ═══════════════ 시나리오 C — 데이터 활용 (8분판 전용) ═══════════════
    if full:
        head("시나리오 C — 데이터 활용 근거 확인", "목표 7:00 ~ 7:40")
        say("   AI가 지어낸 정보가 아니라 공공데이터에 근거한 결과임을 화면에서 확인시킨다.")
        r, _ = api("GET", "/api/facilities", params={"region": "광주광역시 서구", "limit": 3},
                   headers=SW_AUTH)
        fac = r.json() if r.status_code == 200 else []
        check("데이터 활용 — 복지자원 검색", len(fac) > 0, f"{len(fac)}건")
        for f in fac[:3]:
            say(f"   {mark(True)} {f['name']} | {f['region']} | 출처 {f['source']}")
        r, _ = api("GET", "/api/status", headers=SW_AUTH)
        st = r.json() if r.status_code == 200 else {}
        say(f"   {mark(True)} 적재 현황: {st.get('facilities')}")
        say(f"   {mark(True)} 의도 분류기: {'학습 완료' if st.get('intent_model_loaded') else '미학습'}"
            "  (AI-Hub C-DS01 실데이터 학습)")
        say("   멘트: 후보마다 근거 데이터의 출처를 함께 표시합니다.")
        say("        검증할 수 없는 출력은 내보내지 않는다는 원칙입니다.")

    # ══════════════════════ 마무리 ══════════════════════
    head("마무리", "목표 " + win("2:45 ~ 3:00","7:40 ~ 8:00"))
    r, _ = api("GET", "/api/dashboard", headers=SW_AUTH)
    counts = r.json()["counts"] if r.status_code == 200 else {}
    say(f"   대시보드: {counts}")
    r, _ = api("GET", "/api/audit", headers=SW_AUTH)
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
    ap = argparse.ArgumentParser(description="시연 리허설")
    ap.add_argument("--full", action="store_true",
                    help="본선 Day2 Demo 8분판 (STEP 7 플라이휠 + 시나리오 C 데이터 활용 포함)")
    sys.exit(main(ap.parse_args().full))
