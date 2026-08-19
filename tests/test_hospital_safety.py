"""병원 상태 안전 정책 회귀 검증 — 추정을 확정으로 올리지 않는다.

실행:  .venv/bin/python -m tests.test_hospital_safety

'확인됨' 은 **이번 통화에서 어르신이 병원 이름을 직접 말했을 때만** 붙는다.
과거 이력만으로 고른 후보는 아무리 자주 간 곳이어도 '추정' 이다.

왜 이 파일이 따로 있나. 이력 기반 '확인됨' 은 카드·DB 를 지나 전화 안내
(voice._receipt)까지 흘러가 "○○병원으로 접수했습니다" 로 어르신에게 확정
통보된다. 화면과 달리 통화에는 '추정' 배지를 보여줄 자리가 없다. 그래서
상태 한 글자가 곧 안전 문제이고, 되돌아가면 조용히 깨지므로 못 박아 둔다.
"""
from __future__ import annotations

import datetime
import sys

from donghaenggori.core import db, hospital, pipeline
from donghaenggori.web import voice

BASE_DATE = datetime.date(2026, 7, 7)      # 문서 기준 접수일 (화)
PHONE_MAIN = "010-1234-5678"               # 박순자 — 정형외과 단골 2회
PHONE_NEW = "010-7777-8888"                # 정말순 — 이력 0

results: list[tuple[str, bool, str]] = []


def check(name: str, passed, detail: str = "") -> None:
    results.append((name, bool(passed), detail))


# ── HOSPITAL-SAFE-01 : 직접 언급은 확인됨 ──────────────────────
def case01() -> None:
    """어르신이 이번에 말한 병원은 확정해도 된다 — 본인이 댄 이름이다."""
    p = db.get_profile(PHONE_MAIN)
    res = hospital.suggest(p, "정형외과", today=BASE_DATE, spoken="순천정형외과의원")
    ok = res.status == "확인됨" and res.hospital == "순천정형외과의원"
    check("01 직접 언급 → 확인됨", ok, f"{res.hospital}[{res.status}]")

    r = pipeline.run(PHONE_MAIN, "내일 순천정형외과의원 가려고요.")
    c = r.card
    ok2 = c is not None and c.hospital_status == "확인됨"
    check("01b 파이프라인 직접 언급 유지", ok2,
          f"{c.hospital if c else None}[{c.hospital_status if c else None}]")


# ── HOSPITAL-SAFE-02 : 이력 2회는 추정 ─────────────────────────
def case02() -> None:
    """단골이어도 이번에 간다고 말한 적은 없다. 확정 금지."""
    p = db.get_profile(PHONE_MAIN)
    res = hospital.suggest(p, "정형외과", today=BASE_DATE)      # spoken 없음
    ok = res.status == "추정" and res.hospital is not None
    check("02 이력 2회 → 추정", ok, f"{res.hospital}[{res.status}]")

    # 근거 문구도 확정으로 읽히면 안 된다
    joined = " ".join(res.reasons)
    ok2 = "확인 필요" in joined and "단골로 확인됨" not in joined
    check("02b 근거가 확정으로 읽히지 않음", ok2, joined[:60])


# ── HOSPITAL-SAFE-03 : 전화 안내가 병원을 확정하지 않는다 ───────
def case03() -> None:
    """_receipt 는 '확인됨' 일 때만 병원 이름을 읽는다. 이력 기반이면 안 읽힌다."""
    r = pipeline.run(PHONE_MAIN, "나 모레 저번에 무릎 봐준 데 가야겄어.")
    c = r.card
    say = voice._receipt(r)
    ok = c is not None and c.hospital is not None and c.hospital not in say
    check("03 이력 기반 병원은 TTS에서 확정하지 않음", ok,
          f"병원={c.hospital if c else None} / 안내={say}")

    # 안내 자체는 정상적으로 나가야 한다 — 침묵이 되면 안 된다
    check("03b 접수 안내는 유지", "접수했습니다" in say, say)


# ── HOSPITAL-SAFE-04 : 카드/DB 에도 승격 없음 ──────────────────
def case04() -> None:
    r = pipeline.run(PHONE_MAIN, "나 모레 저번에 무릎 봐준 데 가야겄어.")
    c = r.card
    ok = c is not None and c.hospital_status == "추정"
    check("04 카드 상태 = 추정", ok, f"{c.hospital if c else None}[{c.hospital_status if c else None}]")

    # 항목별 상태(fields)도 같은 값이어야 한다 — 화면이 여기를 읽는다
    fv = c.fields_view()["hospital"] if c else {}
    check("04b fields.hospital 상태 일치", fv.get("status") == "추정", str(fv.get("status")))

    # 저장된 카드 전문에서도 승격되지 않는다
    iid = db.save_intake(c, PHONE_MAIN, "전화", status="접수 대기")
    row = db.get_intake(iid)
    saved = (row or {}).get("card") or {}
    check("04c DB 저장분도 추정", saved.get("hospital_status") == "추정",
          str(saved.get("hospital_status")))


# ── 기존 정책 유지 확인 ────────────────────────────────────────
def case05() -> None:
    """이력 없음은 그대로 '확인 필요', 정책 헤더도 유지."""
    res = hospital.suggest(None, "정형외과", today=BASE_DATE)
    check("05 이력 없음 → 확인 필요", res.status == "확인 필요" and res.hospital is None,
          f"{res.hospital}[{res.status}]")

    # policy 는 파이프라인이 아니라 응답 모델이 싣는다 — 계약을 그 자리에서 본다.
    from donghaenggori.web.api import Policy
    pol = Policy().model_dump()
    ok = pol["medical_judgement"] is False and pol["human_review_required"] is True
    check("05b policy 불변", ok, str(pol))


def case06() -> None:
    """짧은 증상 키가 긴 낱말 안에 박혀 엉뚱한 진료과로 가지 않는다.

    실통화에서 "계단에서 굴러가지고 발목이 다친 거 같은데" 가 **이비인후과**
    로 갔다. '발목이' 안의 '목이' 가 사전 순서상 먼저 걸렸기 때문이다.
    손목·팔목도 같았다.

    진료과는 확정 게이트를 막지 않아서 틀려도 접수는 진행된다. 그래서 더
    위험하다 — 아무도 안 막고 그대로 흘러가 이력 필터를 바꾸고, 다음 접수의
    병원 후보를 엉뚱한 곳으로 만든다.
    """
    from donghaenggori.core import nlu
    for 말, 기대 in (
        ("내가 계단에서 굴러가지고 발목이 다친 거 같은데", "정형외과"),
        ("손목이 시큰거려서 병원 좀", "정형외과"),
        ("팔목이 부었어", "정형외과"),
        ("발목에 멍이 들었어", "정형외과"),
        ("발목을 접질렀어", "정형외과"),
        # 진짜 목은 그대로여야 한다 — 고치다 반대로 넘기면 더 나쁘다.
        ("목이 아파서 못 삼키겄어", "이비인후과"),
        ("목에 뭐가 걸린 거 같어", "이비인후과"),
    ):
        got = nlu.analyze(말).dept
        check(f"06 {말[:18]}", got == 기대, f"{got} (기대 {기대})")


def case07() -> None:
    """뇌졸중 징후를 어르신이 쓰는 말로 잡는다. 그리고 오탐이 없어야 한다.

    '말이 어눌'·'한쪽이 안'·'마비' 는 이미 목록에 있었는데 실제 발화와
    어긋났다. 어르신은 "말이 잘 안 나와", "한쪽 팔에 힘이 안 들어가" 라고
    말한다. 노인 대상 서비스에서 이걸 놓치는 것이 제일 무거운 실패다.

    **오탐이 더 무섭다.** 긴급이면 카드를 아예 만들지 않고 담당자에게
    전화를 돌린다. "한쪽 팔이 뻐근해" 를 긴급으로 잡으면 정형외과 접수가
    응급 전환이 되고, 그런 일이 몇 번 반복되면 아무도 이 판정을 안 믿는다.
    """
    from donghaenggori.core import nlu
    for 말 in ("갑자기 머리가 깨질 듯이 아프고 말이 잘 안 나와",
               "머리가 아프고 한쪽 팔에 힘이 안 들어가",
               "어매가 입이 돌아갔어",
               "손에 힘이 안 들어가고 발음이 이상해",
               "눈앞이 캄캄하고 식은땀이 나"):
        check(f"07 긴급 — {말[:16]}", nlu.analyze(말).urgent, "놓쳤다")

    for 말 in ("머리가 아파서 병원 좀 같이 가주쇼",
               "한쪽 팔이 뻐근해서 정형외과 가야겄어",
               "정신이 없어서 깜빡했네 낼 병원 가야 해",
               "무릎이 아파서 걷기가 힘들어",
               "소변이 자주 마려워서 병원 가야 해"):
        check(f"07 일반 — {말[:16]}", not nlu.analyze(말).urgent, "오탐")


def case08() -> None:
    """증상만 말해도 진료과 후보가 나온다 — 비어 있던 과들.

    '머리가 아파' 는 아무것도 못 잡았다. 사전이 비면 그 자리를 과거 이력이
    채우는데, 새 증상에는 이력이 근거가 못 된다. 머리 아픈 어르신에게
    '정형외과(과거 이력의 진료과를 따름)' 가 뜨고 있었다.

    값은 언제나 '추정' 이다 — 어르신이 진료과를 직접 말했을 때만 확인됨이고,
    증상에서 우리가 고른 것은 근거를 대고 제시하는 후보다.
    """
    from donghaenggori.core import nlu
    for 말, 기대 in (("머리가 아파서 병원 좀", "신경과"),
                     ("두통이 심해서", "신경과"),
                     ("손발이 저려서", "신경과"),
                     ("잠이 안 와서 힘들어", "정신건강의학과"),
                     ("물리치료 받으러", "재활의학과"),
                     ("전립선 때문에", "비뇨기과")):
        a = nlu._rule_based(말)
        check(f"08 {말[:14]}", a.dept == 기대, f"{a.dept} (기대 {기대})")
        check(f"08 {말[:14]} 는 추정", a.dept_source == "dict", str(a.dept_source))


def case09() -> None:
    """외출 참고는 **출발지** 기준이고, 주소가 없어도 들은 주소로 낸다.

    모든 어르신이 주소가 등록돼 있지는 않다. 미등록 번호는 프로필이 없고,
    등록돼 있어도 region 이 빈 경우가 있다. 예전에는 그때 체크리스트가
    통째로 사라졌다 — 정작 그런 통화에서는 성함·읍면동을 따로 물어 받아
    두고도 쓰지 않고 있었다.

    가는 병원 기준이 아니다. 카드를 만드는 시점에 병원은 아직 '추정'이거나
    비어 있어서, 확정되지 않은 병원 좌표로 날씨를 뽑으면 틀린 곳의 날씨를
    확신 있게 보여주게 된다.
    """
    from donghaenggori.core import geo

    # 전남 시 지역이 통째로 빠져 있었다 — 목포·여수·순천·나주·광양.
    for 지역 in ("목포시 용당동", "전남 목포시", "여수시", "순천시 조례동",
                 "나주시", "광양시", "전남 고흥군 ○○면"):
        check(f"09 좌표 — {지역}", geo.coords_of(지역) is not None, "못 찾음")

    # 다른 시도 주소를 우리 지역으로 끌어오면 안 된다.
    check("09 서울 주소는 안 잡는다", geo.coords_of("강남구 역삼동") is None,
          str(geo.coords_of("강남구 역삼동")))

    # 폴백 사슬 — 등록 거주지 → 통화에서 들은 주소 → 없으면 안 낸다.
    from donghaenggori.core import pipeline
    from donghaenggori.services import airquality, weather
    원래w, 원래a = weather.checklist, airquality.checklist
    weather.checklist = lambda lat, lon, d=None: ["강수확률 70% → 우산·미끄럼 주의"]
    airquality.checklist = lambda region: []
    try:
        등록 = pipeline.run("010-1234-5678", "낼 병원 가야 해",
                            channel="전화").card.to_dict()["outing_checklist"]
        check("09 등록 거주지 기준", any("등록된 거주지" in x for x in 등록), str(등록))

        들은 = pipeline.run("010-7777-0000", "무릎이 아파서 낼 병원 좀", channel="전화",
                            identity_utterance="이영희요 목포시 용당동 삽니다"
                            ).card.to_dict()["outing_checklist"]
        check("09 들은 주소로도 낸다", any("들은 주소" in x for x in 들은), str(들은))

        없음 = pipeline.run("010-7777-0001", "무릎이 아파서 낼 병원 좀",
                            channel="전화").card.to_dict()["outing_checklist"]
        check("09 위치를 모르면 안 낸다", 없음 == [], str(없음))
    finally:
        weather.checklist, airquality.checklist = 원래w, 원래a


def main() -> int:
    db.init_db()
    for fn in (case01, case02, case03, case04, case05, case06, case07, case08,
               case09):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"예외: {type(e).__name__}: {e}")

    print(f"\n병원 상태 안전 정책 검증 (기준일 {BASE_DATE})")
    print("=" * 92)
    passed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        passed += ok
        print(f"  [{mark}] {name:<38} {detail}")
    print("=" * 92)
    print(f"  {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
