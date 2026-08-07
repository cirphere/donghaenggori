"""의도·긴급 분류기 학습 엔트리포인트.

    python -m donghaenggori.services.train_intent            # 합성 데이터(기준선)
    python -m donghaenggori.services.train_intent --data <경로>   # C-DS01 실데이터

⚠ 기본값인 합성 데이터는 **파이프라인 검증용**이다. 템플릿에서 생성하므로
   정확도가 1.0에 가깝게 나오지만 실제 성능이 아니다.
   실측 수치는 AI-Hub C-DS01(복지 콜센터 상담데이터)로 학습해야 나온다.

학습 산출물(data/models/*.pkl)은 저장소에 포함하지 않는다 — 이 스크립트로 재생성한다.
"""
from __future__ import annotations

import argparse
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
            "약 받으러 가야{e}", "약이 떨어졌{e}"],
    "보호자연락": ["딸한테 연락 좀 해{e}", "아들한테 전화 좀 해{e}", "자식한테 알려{e}",
                 "보호자한테 연락해{e}", "가족한테 좀 알려{e}"],
    "긴급": ["가슴이 답답하고 숨이 차{e}", "숨쉬기 힘들어{e}", "쓰러질 것 같아{e}",
            "어지러워서 못 일어나{e}", "가슴이 아파{e}", "말이 어눌해지{e}",
            "한쪽이 안 움직여{e}", "피가 나{e}"],
}
_COUNTS = {"병원동행": 2000, "약국": 700, "보호자연락": 700, "긴급": 700}


def synthetic(seed: int = 0) -> tuple[list[str], list[str]]:
    rnd = random.Random(seed)
    X, y = [], []
    for label, templates in _TEMPLATES.items():
        for _ in range(_COUNTS[label]):
            t = rnd.choice(templates).format(
                d=rnd.choice(_DEPTS), s=rnd.choice(_SYMPTOMS), e=rnd.choice(_ENDINGS))
            X.append(t)
            y.append(label)
    return X, y


def load_cds01(path: str) -> tuple[list[str], list[str]]:
    """AI-Hub C-DS01 로더 — 데이터 확보 후 실제 스키마에 맞춰 구현한다."""
    raise NotImplementedError(
        f"C-DS01 파서 미구현: {path}\n"
        "AI-Hub 복지 콜센터 상담데이터를 내려받은 뒤 발화/의도 라벨 스키마에 맞춰 구현하세요.")


def main() -> None:
    ap = argparse.ArgumentParser(description="의도·긴급 분류기 학습")
    ap.add_argument("--data", help="C-DS01 데이터 경로 (없으면 합성 데이터)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.data:
        X, y = load_cds01(args.data)
        note = f"C-DS01 실데이터 ({args.data})"
    else:
        X, y = synthetic(args.seed)
        note = "합성 데이터 — 파이프라인 검증용, 실제 성능 아님"

    print(f"학습 데이터: {len(X):,}건 · {note}")
    report = intent_model.train(X, y)
    print()
    print(report.summary())
    print()
    print(f"저장: {intent_model.INTENT_PATH}")
    print(f"      {intent_model.URGENT_PATH}")


if __name__ == "__main__":
    main()
