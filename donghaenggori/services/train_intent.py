"""의도·긴급 분류기 학습 엔트리포인트.

    python -m donghaenggori.services.train_intent                      # 합성만 (기준선)
    python -m donghaenggori.services.train_intent --data <C-DS01 경로>  # 실데이터 + 합성

데이터 구성
  · 병원동행 / 긴급 → AI-Hub C-DS01 실데이터 (복지 콜센터 상담 통화)
  · 약국 / 보호자연락 → 합성 데이터
    C-DS01에 대응 카테고리가 없다. 없는 라벨을 지어내지 않고, 규칙 사전 기반
    합성 문장으로 보완한 뒤 이 사실을 리포트에 명시한다.

C-DS01 주의사항
  · 라벨이 '통화 단위'인데 JSON은 '발화 단위'다. 발화 단위로 학습하면
    차량요청 통화 속 잡담까지 병원동행이 되어 노이즈가 크다 → 세션 단위로 묶는다.
  · 통화 전체를 넣으면 도메인 단어가 그대로 노출돼 정확도가 1.0으로 뜬다.
    실사용(어르신의 짧은 첫 발화)과 무관하므로 **도입부 N발화**만 쓴다.
  · 우리 서비스 입력은 한 문장에 가까우므로 짧은 합성 발화를 함께 섞는다.

학습 산출물(data/models/*.pkl)은 저장소에 포함하지 않는다 — 이 스크립트로 재생성한다.
"""
from __future__ import annotations

import argparse
import collections
import random

from . import intent_model

_ENDINGS = ["어", "겄어", "는디", "요", "", "네", "ㅆ어"]
_DEPTS = ["정형외과", "내과", "안과", "치과", "이비인후과", "재활의학과", "피부과", "신경과"]
_SYMPTOMS = ["무릎", "허리", "눈", "이", "귀", "어깨", "다리", "속"]

_TEMPLATES = {
    "병원동행": ["{d} 가야{e}", "모레 {d} 가야{e}", "저번에 {s} 봐준 데 또 가야{e}",
                "다음주에 {d} 좀 가야{e}", "{s} 아파서 {d} 가야{e}", "병원 좀 델꼬 가야{e}",
                "{d} 예약 있어서 가야{e}", "지난번 그 병원 또 가야{e}"],
    "약국": ["약 타러 가야{e}", "약국 들러야{e}", "처방받은 약 받아야{e}",
            "약 받으러 가야{e}", "약이 떨어졌{e}", "약국도 같이 들러야{e}"],
    "보호자연락": ["딸한테 연락 좀 해{e}", "아들한테 전화 좀 해{e}", "자식한테 알려{e}",
                 "보호자한테 연락해{e}", "가족한테 좀 알려{e}", "딸한테 알려주면 좋겄{e}"],
    "긴급": ["가슴이 답답하고 숨이 차{e}", "숨쉬기 힘들어{e}", "쓰러질 것 같아{e}",
            "어지러워서 못 일어나{e}", "가슴이 아파{e}", "말이 어눌해지{e}",
            "한쪽이 안 움직여{e}", "피가 나{e}"],
}
# 실데이터가 있는 클래스는 적게, 없는 클래스는 충분히
_COUNTS_STANDALONE = {"병원동행": 2000, "약국": 700, "보호자연락": 700, "긴급": 700}
_COUNTS_SUPPLEMENT = {"병원동행": 400, "약국": 400, "보호자연락": 400, "긴급": 300}


def synthetic(counts: dict[str, int], seed: int = 0) -> tuple[list[str], list[str]]:
    rnd = random.Random(seed)
    X, y = [], []
    for label, n in counts.items():
        for _ in range(n):
            t = rnd.choice(_TEMPLATES[label]).format(
                d=rnd.choice(_DEPTS), s=rnd.choice(_SYMPTOMS), e=rnd.choice(_ENDINGS))
            X.append(t)
            y.append(label)
    return X, y


def main() -> None:
    ap = argparse.ArgumentParser(description="의도·긴급 분류기 학습")
    ap.add_argument("--data", help="C-DS01 라벨 디렉터리 (없으면 합성 데이터만)")
    ap.add_argument("--opening", type=int, default=5,
                    help="C-DS01 세션에서 사용할 도입부 고객 발화 수 (기본 5)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    sources: dict[str, str] = {}
    if args.data:
        from . import cds01
        Xr, yr, rep = cds01.load_sessions(args.data, opening=args.opening)
        Xs, ys = synthetic(_COUNTS_SUPPLEMENT, args.seed)
        X, y = Xr, yr          # 평가는 실데이터로만
        AUX = (Xs, ys)         # 합성은 학습에만 투입
        real = collections.Counter(yr)
        for lab in intent_model.INTENTS:
            sources[lab] = f"실데이터 {real.get(lab,0):,} + 합성 {_COUNTS_SUPPLEMENT[lab]:,}" \
                if real.get(lab) else f"합성 {_COUNTS_SUPPLEMENT[lab]:,} (C-DS01에 대응 카테고리 없음)"
        head = (f"C-DS01 세션 {len(Xr):,}건(도입부 {args.opening}발화) 평가 기준 "
                f"+ 합성 {len(Xs):,}건(학습 전용)")
        detail = rep.summary()
    else:
        X, y = synthetic(_COUNTS_STANDALONE, args.seed)
        AUX = (None, None)
        for lab in intent_model.INTENTS:
            sources[lab] = f"합성 {_COUNTS_STANDALONE[lab]:,}"
        head = "합성 데이터만 — 파이프라인 검증용, 실제 성능 아님"
        detail = ""

    print(f"학습 데이터: {len(X):,}건 · {head}")
    if detail:
        print()
        print(detail)
    print()
    print("클래스별 출처:")
    for lab, src in sources.items():
        print(f"  {lab:<8} {src}")
    print()

    report = intent_model.train(X, y, save=not args.no_save,
                                aux_texts=AUX[0], aux_labels=AUX[1])
    print(report.summary())
    if not args.no_save:
        print()
        print(f"저장: {intent_model.INTENT_PATH}")
        print(f"      {intent_model.URGENT_PATH}")


if __name__ == "__main__":
    main()
