"""저확신 폴백 재현율 — 제출 문서(파일1) 4-2 지표.

문서에 적힌 정의를 그대로 잰다.
    지표      확신도 낮을 때 "확인 필요"로 표시한 비율
    측정 방법  오답 케이스 20건 주입 테스트
    본선 목표  0.95 이상

파일1 4-2 의 안전 지표 우선 원칙이 이 지표를 상위에 둔다 — "틀린 답을 확신
있게 말하는 것보다, 모를 때 '확인 필요'라고 표시하는 것이 병원동행 업무에서
더 중요하기 때문". 병원을 잘못 잡으면 어르신이 반나절을 헛걸음한다.

**대조군을 함께 잰다.** 재현율만 재면 전부 "확인 필요"라고 답하는 시스템이
1.000 을 받는다. 그건 지표를 만족시키면서 서비스를 망가뜨리는 방법이고,
심사에서 제일 먼저 나올 질문이다. 그래서 막으면 안 되는 정상 접수도 같이
넣어 **오차단 0건**을 함께 보인다.

실행:  PYTHONPATH=. python tests/metrics_fallback_recall.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DONGHAENGGORI_DB",
                      os.path.join(tempfile.mkdtemp(prefix="fallback-metric-"), "m.db"))

from donghaenggori.core import db, gate, pipeline  # noqa: E402

TARGET = 0.95      # 파일1 4-2 본선 목표

# ── 오답 케이스 20건 ────────────────────────────────────────────────
# "오답 케이스"는 **확신 있게 답하면 틀리는** 상황이다. 각 건마다 어느 항목이
# 막혀야 하는지를 함께 적는다 — 아무 데나 하나 걸리면 통과시키면, 엉뚱한
# 항목이 막힌 것을 맞았다고 세게 된다.
#
# (전화번호, 발화, 기대 항목, 설명, identity_denied)
CASES = [
    # 근거 없음 — 이력이 하나도 없는 대상자
    ("010-7777-8888", "정형외과 가야 해", "hospital", "이력 없음 — 정형외과", False),
    ("010-3562-3157", "치과 좀 가야 쓰겄어", "hospital", "이력 없음 — 치과", False),
    ("010-8736-7732", "내일 병원 좀 가야 해", "hospital", "이력 없음 — 병원만 언급", False),

    # 발화 진료과와 이력이 어긋남 — 이력을 우선시하면 엉뚱한 병원으로 배차된다
    ("010-4218-8885", "정형외과 가야겄어", "hospital", "피부과 이력뿐인데 정형외과", False),
    ("010-6585-4286", "안과 좀 가야 해", "hospital", "신경과 이력뿐인데 안과", False),
    ("010-2222-3333", "피부과 가야 해", "hospital", "정형·재활 이력인데 피부과", False),
    ("010-3855-9490", "정형외과 가야겄어", "hospital", "치과 이력뿐인데 정형외과", False),
    ("010-5457-5765", "신경과 좀 가야 해", "hospital", "치과 이력뿐인데 신경과", False),
    ("010-6015-6195", "안과 가야 쓰겄는디", "hospital", "치과 이력뿐인데 안과", False),
    ("010-5018-8226", "피부과 가야 해", "hospital", "신경과 이력뿐인데 피부과", False),

    # 대상자를 특정할 수 없음 — 번호로 사람을 확정하지 않는다
    ("010-0000-0000", "내일 정형외과 가야 해", "target", "미등록 번호", False),
    ("010-1111-2222", "모레 치과 가야 쓰겄어", "target", "미등록 번호 2", False),
    ("010-9876-5432", "우리 어매 정형외과 좀 델꼬 가야 쓰겄는디", "target",
     "보호자 번호로 대리 접수", False),
    ("010-1234-5678", "모레 정형외과 가야 해", "target",
     "번호 주인이 아니라고 밝힘(2번)", True),

    # 시각이 모호함 — 오전·오후를 우리가 고르면 절반 확률로 반나절을 버린다
    ("010-1234-5678", "모레 3시에 정형외과 가야 해", "time", "3시 — 오전·오후 불명", False),
    ("010-4218-8885", "내일 5시에 피부과 가야 해", "time", "5시 — 오전·오후 불명", False),

    # 날짜가 없거나 모호함 — 날짜 없는 일정은 세울 수 없다
    ("010-1234-5678", "정형외과 가야 해", "date", "날짜 언급 없음", False),
    ("010-6585-4286", "신경과 좀 가야겄어", "date", "날짜 언급 없음 2", False),
    ("010-1234-5678", "다음 주에 정형외과 가야 해", "date", "'다음 주' — 날짜 특정 불가", False),
    ("010-3855-9490", "조만간 치과 가야 쓰겄어", "date", "'조만간' — 날짜 특정 불가", False),
]

# ── 대조군 ──────────────────────────────────────────────────────────
# 막으면 안 되는 접수. 여기서 하나라도 막히면 재현율 숫자는 의미를 잃는다.
CONTROLS = [
    ("010-1234-5678", "모레 오후 2시에 정형외과 가야 해", "단골 + 날짜 + 시각 명시"),
    ("010-4218-8885", "내일 오전 10시에 피부과 가야 해", "단골 피부과 + 날짜 + 시각"),
    ("010-6585-4286", "모레 오후 3시에 신경과 가야 쓰겄어", "단골 신경과 + 날짜 + 시각"),
    ("010-3855-9490", "내일 오후 1시에 치과 가야 해", "단골 치과 + 날짜 + 시각"),
    ("010-5457-5765", "모레 오전 11시에 치과 좀 가야 해", "단골 치과 + 날짜 + 시각"),
]


def _blocked(phone: str, utterance: str, denied: bool = False) -> list[str]:
    r = pipeline.run(phone, utterance, channel="전화", identity_denied=denied)
    if r.urgent or not r.card:
        # 긴급은 접수카드를 만들지 않고 사람에게 넘긴다 — 이것도 폴백이다.
        return ["__urgent__"]
    return [b["field"] for b in gate.blockers(r.card.to_dict())]


def main() -> int:
    db.init_db()
    print("=" * 78)
    print("  저확신 폴백 재현율 — 파일1 4-2 지표")
    print("=" * 78)
    print(f"  오답 케이스 {len(CASES)}건 주입 · 대조군 {len(CONTROLS)}건")
    print()

    print("  " + "-" * 74)
    print(f"  {'설명':<30}{'기대':<10}{'실제로 막힌 항목':<24}판정")
    print("  " + "-" * 74)
    hits = 0
    for phone, utt, expect, label, denied in CASES:
        blocked = _blocked(phone, utt, denied)
        ok = expect in blocked
        hits += ok
        print(f"  {label:<30}{expect:<10}{','.join(blocked) or '(없음)':<24}"
              f"{'통과' if ok else '실패'}")
    print("  " + "-" * 74)
    recall = hits / len(CASES)
    print(f"  재현율 {hits}/{len(CASES)} = {recall:.3f}")
    print()

    print("  " + "-" * 74)
    print("  대조군 — 막히면 안 되는 정상 접수")
    print("  " + "-" * 74)
    false_blocks = 0
    for phone, utt, label in CONTROLS:
        blocked = _blocked(phone, utt)
        bad = bool(blocked)
        false_blocks += bad
        print(f"  {label:<44}{','.join(blocked) or '(안 막힘)':<20}"
              f"{'오차단' if bad else '정상'}")
    print("  " + "-" * 74)
    print(f"  오차단 {false_blocks}/{len(CONTROLS)}건")
    print()

    print("  " + "-" * 74)
    print("  고친 것 — 대리 접수 1건")
    print("  " + "-" * 74)
    print("  예전에는 이 건의 대상자가 '추정'이라 게이트를 통과했다. 표시 문자열은")
    print("  '박순자 (보호자 대리 요청 — 확인 필요)' 인데 status 는 '추정' 이어서,")
    print("  화면은 확인하라고 말하고 게이트는 통과시키는 상태였다.")
    print()
    print("  근거가 '이 번호로 등록된 사람이 한 명' 뿐인데, 그 한 명은 보호자가")
    print("  앞서 신청해 등록된 어르신일 뿐이다. 같은 보호자가 다른 부모를 신청하면")
    print("  두 번째 접수가 첫 어르신 이름으로 확정된다 — 재현했다. 병원을 잘못")
    print("  고르면 헛걸음이지만 대상자를 잘못 고르면 남의 기록이 된다.")
    print()
    print("  이제 '확인 필요' 다(pipeline.py). 사회복지사가 '맞으실까요' 를 묻고")
    print("  확인해야 확정된다. 그 질문은 접수카드가 이미 만들어 둔다.")
    print()

    ok = recall >= TARGET and false_blocks == 0
    print("=" * 78)
    print(f"  본선 목표 {TARGET:.2f} 대비 — 재현율 {recall:.3f} "
          f"{'달성' if recall >= TARGET else '미달'}"
          f" · 오차단 {false_blocks}건")
    if not false_blocks:
        print("  → 전부 '확인 필요'로 답해서 얻은 숫자가 아니다. 정상 접수는 그대로 지나간다.")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
