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
PHONE = "010-1234-5678"        # 박순자 — 정형외과 2회 이력 → 추정(직접 말한 것이 아니므로)
PHONE_NEW = "010-0000-0000"    # 미등록
GUARDIAN = "010-9876-5432"     # 박순자의 딸
PHONE_FLYWHEEL = "010-2222-3333"   # 김수남 — 정형외과 1회 (참고용)
# 플라이휠 시연 전용. **시드에 없는 번호여야** 1차 접수가 "이력 없음"이 된다.
PHONE_NEW_FLYWHEEL = "010-3333-7788"
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


def api(method: str, path: str, timeout: float = 30, **kw):
    """리허설용 HTTP 호출. 기본 30초, 오래 걸리는 곳은 호출부에서 늘린다.

    예열(/api/warmup)은 30초로 안 된다 — 음성인식·의도분류·임베딩 모델을 전부
    적재하고 외부 API 캐시까지 채운다. 첫 기동이면 모델 다운로드까지 붙는다.
    실제로 여기서 ReadTimeout 으로 리허설이 통째로 죽었다(preflight 는 처음부터
    600초를 주고 있었는데 이쪽만 30초였다).
    """
    import httpx
    t = time.time()
    r = httpx.request(method, BASE + path, timeout=timeout, **kw)
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
    sw_id, sw_pw = os.environ.get("DEMO_SW_ID"), os.environ.get("DEMO_SW_PASSWORD")
    mgr_id, mgr_pw = os.environ.get("DEMO_MGR_ID"), os.environ.get("DEMO_MGR_PASSWORD")
    # 초기화는 관리자만 할 수 있다. 없으면 초기화를 건너뛰고 계속한다 —
    # 리허설을 아예 못 돌리는 것보다는 낫지만, 남은 데이터 때문에 감사 로그
    # 건수 같은 검사가 어긋날 수 있어서 크게 알린다.
    adm_id, adm_pw = os.environ.get("DEMO_ADMIN_ID"), os.environ.get("DEMO_ADMIN_PASSWORD")
    if not (sw_id and sw_pw and mgr_id and mgr_pw):
        print("DEMO_SW_ID/DEMO_SW_PASSWORD, DEMO_MGR_ID/DEMO_MGR_PASSWORD 환경변수가 필요합니다.")
        print("  python -m donghaenggori.services.create_user 로 사회복지사·동행매니저 계정을 먼저 만드세요.")
        return 1

    today = datetime.date.today()
    span = "본선 Day2 Demo 8분판" if full else "파일2 대표 시나리오 3분판"
    say(f"\033[1m동행고리 AI — 시연 리허설\033[0m  ({span})")
    say(f"오늘 {today} · 대상자 박순자 · 시드 초기화 후 시작")
    say("=" * 74)

    # 초기화는 **관리자 전용**이 됐다(가장 파괴적인 호출인데 유일하게 무인증이었다).
    # 관리자로 로그인 → 초기화 순이다. 초기화가 세션을 전부 지우므로 아래에서
    # 사회복지사·동행매니저 로그인을 **그다음에** 한다 — 먼저 받아두면 그 자리에서 죽는다.
    if adm_id and adm_pw:
        r, _ = api("POST", "/api/auth/login", json={"user_id": adm_id, "password": adm_pw})
        if r.status_code != 200:
            print(f"관리자 로그인 실패: HTTP {r.status_code} {r.text[:120]}")
            return 1
        rr, _ = api("POST", "/api/reset",
                    headers={"Authorization": f"Bearer {r.json()['token']}"})
        if rr.status_code != 200:
            print(f"초기화 실패: HTTP {rr.status_code} {rr.text[:120]}")
            return 1
    else:
        say("   \033[33m※ DEMO_ADMIN_ID/PASSWORD 가 없어 초기화를 건너뜁니다 —"
            " 남은 데이터 때문에 일부 검사가 어긋날 수 있습니다.\033[0m")

    r, _ = api("POST", "/api/auth/login", json={"user_id": sw_id, "password": sw_pw})
    if r.status_code != 200:
        print(f"사회복지사 로그인 실패: HTTP {r.status_code} {r.text[:120]}")
        return 1
    SW_AUTH = {"Authorization": f"Bearer {r.json()['token']}"}
    r, _ = api("POST", "/api/auth/login", json={"user_id": mgr_id, "password": mgr_pw})
    if r.status_code != 200:
        print(f"동행매니저 로그인 실패: HTTP {r.status_code} {r.text[:120]}")
        return 1
    MGR_AUTH = {"Authorization": f"Bearer {r.json()['token']}"}

    # 예열 — 외부 API 캐시를 미리 채워 시연 중 지연을 없앤다
    say("   예열 중 — 모델 적재와 외부 API 캐시. 첫 기동이면 수 분 걸린다…")
    rw, elw = api("POST", "/api/warmup", timeout=600, headers=SW_AUTH)
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
    r, el = api("POST", "/api/intakes", timeout=180,
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
    # 확신도는 규칙 폴백일 때 None 이다. :.2f 로 바로 찍으면 TypeError 로 죽는데,
    # 하필 **모델이 안 올라간 상태**에서만 그렇다 — 폴백이 도는지 확인하려고
    # 돌리는 스크립트가 그때 죽으면 앞뒤가 안 맞는다.
    conf = d.get("intent_confidence")
    say(f"   {mark(True)} 의도     : {d['intent']}  "
        f"({d['intent_source']}{f' {conf:.2f}' if conf is not None else ''})")
    say(f"   {mark(True)} 병원 후보: {c['hospital']}  [{c['hospital_status']}]")
    say(f"                근거: {c['reasons'][0] if c['reasons'] else '—'}")
    say(f"   {mark(True)} 방문 예정: {c['date_label']} → {c['date_value']}")
    say(f"   {mark(True)} 동행 수준: {c['need_level']} ({', '.join(c['need_reasons'])})")
    if c["outing_checklist"]:
        for x in c["outing_checklist"]:
            say(f"   {mark(True)} 외출 전  : {x}")
    say(f"   소요 {el:.1f}s")

    # 이력에서 고른 병원은 '추정' 이다 — 어르신이 이번에 그 이름을 말하지 않았다.
    # 후보와 근거는 그대로 나와야 한다. 후보가 사라지면 Care Memory 가 죽은 것이고,
    # '확인됨' 이 되면 말한 적 없는 병원을 확정한 것이다. 둘 다 검사한다.
    check("병원 후보 = 추정(이력 기반)",
          c["hospital"] is not None and c["hospital_status"] == "추정",
          f"{c['hospital']} [{c['hospital_status']}]")
    check("병원 후보 근거 제시", bool(c["reasons"]),
          c["reasons"][0] if c["reasons"] else "근거 없음")
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
    #
    # **보여줄 것은 "정확도가 오른다" 가 아니라 "물어볼 것이 줄어든다" 다.**
    #
    # 예전에는 2회 방문이 되면 병원이 '추정 → 확인됨' 으로 올라가는 것을 보여줬다.
    # 그 승격은 안전 문제로 없앴다(#55) — 어르신이 말하지 않은 병원을 확정해서
    # 전화 안내로 들려주고 있었다. 그래서 장면이 비었다.
    #
    # 대신 게이트가 막는 항목 수를 센다. 이력이 없으면 대상자·병원·방문일 셋을
    # 물어야 하고, 이력이 쌓이면 방문일 하나만 남는다. 복지사가 실제로 겪는
    # 변화가 그것이고, 숫자로 보인다.
    if full:
        say()
        say("\033[1mSTEP 7\033[0m  쓸수록 물어볼 것이 줄어든다 — 플라이휠        (4:40~5:20)")

        u = "허리가 아파서 정형외과 가야 하는디"
        def intake_of(phone: str) -> tuple[int, list[str], dict]:
            r, _ = api("POST", "/api/intakes",
                       json={"phone": phone, "utterance": u}, headers=SW_AUTH)
            body = r.json()
            iid = body.get("intake_id")
            d, _ = api("GET", f"/api/intakes/{iid}", headers=SW_AUTH)
            g = d.json().get("gate") or {}
            return iid, [b["label"] for b in g.get("blockers", [])], body.get("card") or {}

        # ① 이력이 없는 어르신 — 처음 겪는 접수
        iid1, first, _ = intake_of(PHONE_NEW_FLYWHEEL)
        say(f'   1차 접수(이력 없음): "{u}"')
        say(f"   {mark(True)} 확인할 항목 {len(first)}개 — {', '.join(first)}")
        say("                 병원도 대상자도 우리가 아는 것이 없다")

        # ② 확인 → 확정 → 동행 → 사후기록.
        #
        # **이력만 넣어서는 안 된다.** 미등록 번호는 케어 프로필이 없어서
        # 병원 후보 산출이 이력을 아예 보지 못한다. 확정이 프로필을 만들고,
        # 그 프로필에 이력이 붙는다 — 이 순서가 곧 서비스의 데이터 순환이다.
        today = datetime.date.today().isoformat()
        # 1차에서 물어야 했던 것을 전부 확인한다 — 이것이 복지사가 실제로 거는
        # 확인 전화다. 대상자만 확인하고 확정을 시도하면 병원에서 409 로 막힌다.
        for field, value in (("target", "정복순"), ("hospital", "△△정형외과"),
                             ("date", today)):
            api("POST", f"/api/intakes/{iid1}/verify",
                json={"field": field, "value": value}, headers=SW_AUTH)
        cr, _ = api("POST", f"/api/intakes/{iid1}/confirm",
                    json={"hospital": "△△정형외과", "date": today, "level": "동행 필요"},
                    headers=SW_AUTH)
        check("STEP 7 1차 — 확인 후 확정", cr.status_code == 200, f"HTTP {cr.status_code}")
        say(f"   {mark(cr.status_code == 200)} 통화로 확인({len(first)}건) → 확정 → 케어 프로필 등록")
        api("POST", "/api/flywheel",
            json={"phone": PHONE_NEW_FLYWHEEL, "date": today,
                  "hospital": "△△정형외과", "dept": "정형외과"}, headers=SW_AUTH)
        say(f"   {mark(True)} 동행 완료 → 사후기록 → 이력에 1건 누적")

        # ③ 같은 발화, 같은 어르신 — 이번엔 프로필과 이력이 있다
        _, second, c2 = intake_of(PHONE_NEW_FLYWHEEL)
        say(f'   2차 접수(이력 1건): "{u}"')
        say(f"   {mark(True)} 확인할 항목 {len(second)}개 — {', '.join(second) or '없음'}")
        if c2.get("hospital"):
            say(f"                 병원 후보: {c2['hospital']} [{c2['hospital_status']}]")
            say(f"                 근거: {(c2.get('reasons') or ['—'])[0]}")

        shrank = len(second) < len(first)
        check("STEP 7 플라이휠 — 확인할 항목이 줄어든다", shrank,
              f"{len(first)}개({','.join(first)}) → {len(second)}개({','.join(second) or '없음'})")

        # ④ 오래 다닌 어르신과 비교 — 도달점을 보여준다
        _, veteran, _ = intake_of(PHONE)
        say(f"   {mark(True)} 참고: 이력 3건인 어르신은 확인할 항목 {len(veteran)}개 "
            f"— {', '.join(veteran) or '없음'}")
        check("STEP 7 이력이 많을수록 더 줄어든다", len(veteran) <= len(second),
              f"이력1건 {len(second)}개 · 이력3건 {len(veteran)}개")

        say("   멘트: 쌓이는 것은 확신이 아니라 근거입니다. 병원은 여전히 '추정'이고")
        say("         확정은 사람이 합니다. 대신 물어볼 것이 셋에서 하나로 줍니다.")

    # ══════════════════════ 시나리오 B — 실패 대응 ══════════════════════
    head("시나리오 B — 실패·저확신 대응", "목표 " + win("2:00 ~ 2:45","5:20 ~ 7:00"))

    # B-1 정보 부족
    say("\033[1mB-1\033[0m  이력에 없는 요청                                 (" + win("2:00~2:15","5:20~5:55") + ")")
    r, el = api("POST", "/api/intakes", timeout=180,
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
        # 대상자가 사는 지역으로 조회한다. 예전에는 "광주광역시 서구" 를 박아
        # 두어서, 고흥 어르신 이야기를 하다가 갑자기 광주 복지관이 나왔다 —
        # 심사에서 "고흥 어르신인데 왜 광주?" 로 이어질 자리였다.
        #
        # 실제 서비스(rag.enrich)는 처음부터 프로필의 region 을 쓴다. 여기만
        # 시연 서사와 어긋나 있었다.
        # **시군까지 다 적어야 한다.** 이 API 는 rag.search 를 타는데, 거기서는
        # 지역을 토큰 단위로 정확히 맞춘다 — '고흥' 은 시설의 '고흥군' 과 다른
        # 토큰이라 0건이 나온다(db.search_facilities 의 LIKE 부분일치와 다르다).
        # 그 엄격함은 의도된 것이다: 느슨하게 맞추면 '전남' 하나로 신안군 섬
        # 어르신에게 100km 떨어진 고흥군 복지관이 '관내' 로 뜬다(rag.py 주석).
        r, _ = api("GET", "/api/facilities", params={"region": "전남 고흥군", "limit": 3},
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
