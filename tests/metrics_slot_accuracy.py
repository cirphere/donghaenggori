"""슬롯 일치율 — 발화의 핵심 단어가 접수카드에 그대로 들어갔는가.

WER(단어 오류율)을 재지 않는 이유가 있다. 이 서비스에서 중요한 것은 문장을
얼마나 똑같이 받아적었는가가 아니라, **일정을 세우는 데 필요한 단어를
지켰는가**다. "모레 저번에 무릎 봐준 데 가야겄어" 를 "모레 저번에 무릎
봐준 데 가야겠어" 로 받아도 서비스는 멀쩡하다. 반대로 "모레" 를 "그저께" 로
받으면 WER 은 거의 그대로인데 어르신이 이틀 전에 병원 앞에 서게 된다.

그래서 슬롯 단위로 잰다.

    대상자 · 병원 · 진료과 · 방문일 · 방문 시각

세 갈래로 나눠 센다. 이 구분이 핵심이다 —

    적중   기대한 값이 카드에 그대로 들어갔다
    보류   '확인 필요' 로 남겨 사람에게 넘겼다 (틀린 것이 아니다)
    오답   **다른 값을 확신 있게 넣었다** ← 유일하게 사고로 이어지는 칸

오답만이 진짜 실패다. 보류는 확인 전화 한 통이지만 오답은 헛걸음이다.
그래서 목표를 둘로 나눈다.

    슬롯 적중률   0.80 이상   (물어보지 않고 채운 비율)
    슬롯 오답     0건         ← 이쪽이 상위 목표다

방언을 따로 센다. 전남 어르신이 실제로 쓰는 어미("가야겄어", "쓰겄는디",
"허요")가 섞였을 때 표준어 대비 얼마나 떨어지는지를 보여야, 지역 서비스라는
말이 근거를 갖는다.

실행:  PYTHONPATH=. python tests/metrics_slot_accuracy.py
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from donghaenggori.core import pipeline  # noqa: E402

# 기준일을 박지 않는다. 파이프라인이 실행일을 쓰므로, 기대값도 실행일에서
# 계산해야 지표가 날짜 지나도 썩지 않는다.
TODAY = datetime.date.today()
TARGET_HIT = 0.80


def offset(days: int) -> str:
    return (TODAY + datetime.timedelta(days=days)).isoformat()


def next_weekday(weekday: int) -> str:
    """다음 주의 해당 요일. 월=0 … 일=6 (dateparse 의 '다음주 X요일' 과 맞춘다)."""
    ahead = (weekday - TODAY.weekday()) % 7 or 7
    return (TODAY + datetime.timedelta(days=ahead)).isoformat()

# 등록 대상자 — 시드 프로필(박순자, 전남 고흥군, 정형외과 이력 2회)
PHONE = "010-1234-5678"
# 미등록 번호 — 신규(cold start) 경로
PHONE_NEW = "010-0000-0000"

HIT, HOLD, WRONG = "적중", "보류", "오답"


# 케이스: (발화, 방언인가, 번호, {슬롯: 기대값})
#
# 기대값 규칙
#   문자열  그 값이 그대로 들어와야 적중
#   None    '확인 필요' 로 남는 것이 정답 (모르는 것을 모른다고 해야 하는 자리)
#
# 발화는 파일2·3 시나리오와 실제 통화에서 관찰된 표현을 옮겼다. 표준어 문장과
# 방언 문장을 **같은 의미로 짝지어** 넣어, 차이가 어미 때문인지 내용 때문인지
# 헷갈리지 않게 했다.
CASES: list[tuple[str, bool, str, dict[str, str | None]]] = [
    # ── 표준어 ────────────────────────────────────────────────
    ("내일 정형외과 가야 해요", False, PHONE,
     {"date": offset(1), "dept": "정형외과"}),
    ("모레 오후 두시에 정형외과 예약이 있어요", False, PHONE,
     {"date": offset(2), "time": "14:00", "dept": "정형외과"}),
    ("다음주 화요일에 내과 좀 가야 합니다", False, PHONE,
     {"date": next_weekday(1), "dept": "내과"}),
    ("송정병원으로 내일 가려고요", False, PHONE,
     {"date": offset(1), "hospital": "송정병원"}),
    ("무릎이 아파서 병원에 가야 하는데 날짜는 아직 모르겠어요", False, PHONE,
     {"date": None}),
    ("3시에 가야 해요", False, PHONE,
     {"time": None}),                      # 오전·오후 불명 — 물어야 한다
    ("내일 오전 열한시 재활의학과요", False, PHONE,
     {"date": offset(1), "time": "11:00", "dept": "재활의학과"}),

    # ── 전남 방언 (같은 의미의 표준어와 짝) ──────────────────────
    ("낼 정형외과 가야 쓰겄어라", True, PHONE,
     {"date": offset(1), "dept": "정형외과"}),
    ("모레 오후 두시에 정형외과 가야겄어", True, PHONE,
     {"date": offset(2), "time": "14:00", "dept": "정형외과"}),
    ("담주 화요일에 내과 좀 가야 쓰겄는디", True, PHONE,
     {"date": next_weekday(1), "dept": "내과"}),
    ("송정병원으로 낼 갈라고 허요", True, PHONE,
     {"date": offset(1), "hospital": "송정병원"}),
    ("무릎이 아픈디 언제 갈지는 아직 몰것소", True, PHONE,
     {"date": None}),
    ("시방 세시에 가야 쓰겄는디", True, PHONE,
     {"time": None}),                      # 방언 + 한글 수사 + 오전·오후 불명
    ("낼 아침 열한시 재활의학과 가야제", True, PHONE,
     {"date": offset(1), "time": "11:00", "dept": "재활의학과"}),

    # ── 어려운 케이스 ────────────────────────────────────────────
    #
    # **일부러 못 맞힐 만한 것을 넣는다.** 구현을 알고 쓴 쉬운 문장만 모으면
    # 지표가 1.000 이 나오고, 그 숫자는 심사에서 한 번에 반박당한다. 여기 값은
    # "지금 어디까지 되는가"를 보여주는 것이지 자랑하려고 있는 것이 아니다.
    ("낼 말고 모레 정형외과", True, PHONE,
     {"date": offset(2), "dept": "정형외과"}),          # 부정 + 정정
    # ★ 알려진 공백 — 진료과 정정을 못 잡는다. 날짜는 "낼 말고 모레" 를
    #   처리하는데 진료과는 첫 매칭을 그대로 쓴다. 고치면 이 줄이 O 로 바뀐다.
    #   숨기지 않고 남겨 둔다 — 지표는 자랑이 아니라 남은 일의 목록이다.
    ("정형외과 말고 내과로 낼", True, PHONE,
     {"date": offset(1), "dept": "내과"}),
    ("이번주 말고 담주 수요일에", True, PHONE,
     {"date": next_weekday(2)}),                        # 주 단위 정정
    # '낼모레' 는 두 날이 아니라 하루다(=모레). 예전에는 '낼' 과 '모레' 가
    # 각각 후보로 잡혀 '복수 표현' 으로 걸렸고, 이 기대값이 그 동작을 정답으로
    # 굳혀 두고 있었다. '모레쯤' 은 원래도 확정하므로 '낼모레쯤' 만 물어보는
    # 것은 앞뒤가 안 맞았다.
    ("낼모레쯤 가야 쓰겄는디", True, PHONE,
     {"date": offset(2)}),
    ("아침 일찍 가고 잡소", True, PHONE,
     {"time": None}),                                   # 시각 아님
    ("낼 오후에 이비인후과", True, PHONE,
     {"date": offset(1), "dept": "이비인후과", "time": None}),   # '오후'만은 시각 아님
    # 두 자리 시각은 오전으로 확정한다(파서 설계). 오후 10시 반에 병원에
    # 가지는 않으므로, 여기서 되묻는 것은 과잉이라고 본 것이다.
    ("모레 열시 반 신경과 예약", False, PHONE,
     {"date": offset(2), "time": "10:30", "dept": "신경과"}),
    ("담주에 안과 좀 가야 하는디", True, PHONE,
     {"date": None, "dept": "안과"}),                    # 요일 없는 '담주'

    # ── 신규 대상자 (지역 근거가 필요한 자리) ────────────────────
    ("내일 그 큰 병원 좀 가야 쓰겄는디", True, PHONE_NEW,
     {"hospital": None, "target": None}),
    ("병원 좀 데려다 주씨요", True, PHONE_NEW,
     {"hospital": None, "target": None}),
]

_fail = 0
rows: list[tuple[str, bool, str, str, str, str]] = []


def judge(expected: str | None, value, status: str) -> str:
    """기대값과 카드 값을 비교해 적중·보류·오답으로 가른다.

    **'확인 필요'가 정답인 자리에서 확인 필요를 낸 것은 적중이다.** 처음엔
    이것도 보류로 셌는데, 그러면 모르는 것을 모른다고 답한 것이 점수를 깎는다.
    이 서비스가 제일 중요하게 보는 행동을 감점하는 지표는 지표가 아니다.

    보류는 **채울 수 있었는데 못 채운 것**만 뜻한다 — 틀린 건 아니지만
    확인 전화가 한 통 늘어난다.
    """
    if expected is None:
        # 모르는 것을 모른다고 해야 하는 자리. 값을 확신 있게 넣으면 오답이다.
        return HIT if status == "확인 필요" else WRONG
    if status == "확인 필요":
        return HOLD
    got = (value or "").strip()
    return HIT if got == expected else WRONG


def main() -> int:
    global _fail
    print("슬롯 일치율 — 발화의 핵심 단어가 카드에 들어갔는가")
    print(f"기준일 {TODAY} · 케이스 {len(CASES)}건")
    print("=" * 92)
    print(f"  {'발화':38} {'슬롯':7} {'기대':12} {'카드':12} 판정")
    print("-" * 92)

    tally = {HIT: 0, HOLD: 0, WRONG: 0}
    by_dialect: dict[bool, dict[str, int]] = {
        False: {HIT: 0, HOLD: 0, WRONG: 0},
        True: {HIT: 0, HOLD: 0, WRONG: 0},
    }

    for utterance, dialect, phone, expect in CASES:
        res = pipeline.run(phone, utterance, channel="전화",
                           use_llm=False, with_rag=False)
        if res.card is None:                # 긴급이면 카드가 없다 — 이 표엔 없다
            continue
        fields = res.card.to_dict().get("fields", {})
        shown = utterance[:36]
        for slot, want in expect.items():
            f = fields.get(slot) or {}
            verdict = judge(want, f.get("value"), f.get("status") or "확인 필요")
            tally[verdict] += 1
            by_dialect[dialect][verdict] += 1
            if verdict == WRONG:
                _fail += 1
            mark = {HIT: "O", HOLD: "-", WRONG: "X"}[verdict]
            print(f"  {shown:38} {slot:7} {str(want or '확인필요'):12} "
                  f"{str(f.get('value') or '—')[:12]:12} {mark} {verdict}")
            shown = ""

    total = sum(tally.values())
    hit_rate = tally[HIT] / total if total else 0.0
    print("=" * 92)
    print("  O 적중(값이 맞거나, 모를 것을 모른다고 함) · - 보류(채울 수 있었는데 못 채움) · X 오답")
    print()
    print(f"  전체     적중 {tally[HIT]:2}/{total}  보류 {tally[HOLD]:2}  "
          f"오답 {tally[WRONG]:2}   적중률 {hit_rate:.3f}")

    for dialect, label in ((False, "표준어"), (True, "방언  ")):
        d = by_dialect[dialect]
        n = sum(d.values())
        rate = d[HIT] / n if n else 0.0
        print(f"  {label}   적중 {d[HIT]:2}/{n}  보류 {d[HOLD]:2}  "
              f"오답 {d[WRONG]:2}   적중률 {rate:.3f}")

    std = by_dialect[False]
    dia = by_dialect[True]
    std_rate = std[HIT] / sum(std.values()) if sum(std.values()) else 0
    dia_rate = dia[HIT] / sum(dia.values()) if sum(dia.values()) else 0
    print()
    print(f"  방언 격차 {std_rate - dia_rate:+.3f}  "
          "(0 에 가까울수록 방언에서도 같은 품질)")
    print()
    print(f"  목표 적중률 {TARGET_HIT:.2f} 대비 — {hit_rate:.3f} "
          f"{'달성' if hit_rate >= TARGET_HIT else '미달'}")
    print(f"  목표 오답 0건 대비 — {tally[WRONG]}건 "
          f"{'달성' if tally[WRONG] == 0 else '미달'}")
    print()
    print("  오답만이 사고로 이어진다. 보류는 확인 전화 한 통이지만,")
    print("  오답은 어르신이 엉뚱한 날 엉뚱한 병원 앞에 서는 것이다.")
    print()
    print("  알려진 공백: 진료과 정정('정형외과 말고 내과로')을 못 잡는다.")
    print("  날짜는 정정을 처리하는데 진료과는 첫 매칭을 쓴다 — 남은 일이다.")

    # 오답이 있거나 적중률이 목표 미만이면 실패로 센다.
    return 1 if (tally[WRONG] or hit_rate < TARGET_HIT) else 0


if __name__ == "__main__":
    sys.exit(main())
