"""의도 분류기 — 직접 학습하는 AI (md 6-1 '분류(하이브리드 NLU)').

두 개의 모델을 함께 쓴다.
  1) 의도 4분류      병원동행 / 약국 / 보호자연락 / 긴급
  2) 긴급 이진분류    재현율 우선 — 놓치는 것이 오탐보다 훨씬 위험

설계 원칙
  · 학습은 GPU 머신에서, 추론은 CPU(배포 서버·맥북 폴백)에서 돌아가야 한다.
    → TF-IDF + LogisticRegression. 아티팩트가 수 MB, 단건 추론 1ms 미만.
  · 긴급 임계값은 정확도가 아니라 **목표 재현율**로 정한다(안전 지표 우선 원칙).
  · 학습된 모델이 없으면 규칙 기반으로 폴백한다 — 발표 때 막히지 않게.

문자 n-gram을 쓰는 이유: 한국어 구어체·사투리는 형태소 분석기가 자주 깨진다.
("가야겄어", "쓰겄는디" 등) 문자 2~4gram이 어미 변형에 강하다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

INTENTS = ["병원동행", "약국", "보호자연락", "긴급"]
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "models")
INTENT_PATH = os.path.join(MODEL_DIR, "intent_clf.pkl")
URGENT_PATH = os.path.join(MODEL_DIR, "urgent_clf.pkl")

# 긴급 감지 목표 재현율 — 파일1 성능 목표(97% 이상)
TARGET_URGENT_RECALL = 0.97


@dataclass
class TrainReport:
    n_train: int = 0
    n_test: int = 0
    intent_accuracy: float = 0.0
    intent_macro_f1: float = 0.0
    urgent_recall: float = 0.0
    urgent_precision: float = 0.0
    urgent_threshold: float = 0.5
    per_class: dict = field(default_factory=dict)

    def summary(self) -> str:
        L = [
            f"학습 {self.n_train:,}건 / 평가 {self.n_test:,}건",
            f"의도 분류 정확도   : {self.intent_accuracy:.3f}",
            f"의도 macro-F1     : {self.intent_macro_f1:.3f}",
            f"긴급 재현율        : {self.urgent_recall:.3f}  (임계값 {self.urgent_threshold:.2f})",
            f"긴급 정밀도        : {self.urgent_precision:.3f}",
        ]
        for k, v in self.per_class.items():
            L.append(f"  · {k:<8} P={v['precision']:.3f} R={v['recall']:.3f} F1={v['f1-score']:.3f} (n={int(v['support'])})")
        return "\n".join(L)


def _build_pipeline():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    return Pipeline([
        ("tfidf", TfidfVectorizer(
            analyzer="char_wb", ngram_range=(2, 4),
            min_df=2, max_features=300_000, sublinear_tf=True)),
        ("clf", LogisticRegression(
            max_iter=1000, C=4.0, class_weight="balanced")),
    ])


def _tune_threshold(y_true, proba, target_recall: float) -> float:
    """목표 재현율을 만족하는 임계값 중 정밀도가 가장 높은 값을 고른다."""
    import numpy as np
    best_t, best_p = 0.5, -1.0
    for t in np.arange(0.05, 0.96, 0.01):
        pred = proba >= t
        tp = int(((pred == 1) & (y_true == 1)).sum())
        fn = int(((pred == 0) & (y_true == 1)).sum())
        fp = int(((pred == 1) & (y_true == 0)).sum())
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        if recall < target_recall:
            continue
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        if precision > best_p:
            best_p, best_t = precision, float(t)
    return best_t


def train(texts: list[str], labels: list[str], test_size: float = 0.2,
          seed: int = 42, save: bool = True) -> TrainReport:
    """의도 분류기 + 긴급 이진분류기를 학습한다."""
    import joblib
    import numpy as np
    from sklearn.metrics import classification_report, accuracy_score, f1_score
    from sklearn.model_selection import train_test_split

    X_tr, X_te, y_tr, y_te = train_test_split(
        texts, labels, test_size=test_size, random_state=seed, stratify=labels)

    # 1) 의도 4분류
    intent_clf = _build_pipeline()
    intent_clf.fit(X_tr, y_tr)
    pred = intent_clf.predict(X_te)
    rep = classification_report(y_te, pred, output_dict=True, zero_division=0)

    # 2) 긴급 이진분류 — 재현율 우선
    yb_tr = np.array([1 if y == "긴급" else 0 for y in y_tr])
    yb_te = np.array([1 if y == "긴급" else 0 for y in y_te])
    urgent_clf = _build_pipeline()
    urgent_clf.fit(X_tr, yb_tr)
    proba = urgent_clf.predict_proba(X_te)[:, 1]
    thr = _tune_threshold(yb_te, proba, TARGET_URGENT_RECALL)
    ub = proba >= thr
    tp = int((ub & (yb_te == 1)).sum()); fn = int((~ub & (yb_te == 1)).sum())
    fp = int((ub & (yb_te == 0)).sum())

    report = TrainReport(
        n_train=len(X_tr), n_test=len(X_te),
        intent_accuracy=accuracy_score(y_te, pred),
        intent_macro_f1=f1_score(y_te, pred, average="macro", zero_division=0),
        urgent_recall=tp / (tp + fn) if (tp + fn) else 0.0,
        urgent_precision=tp / (tp + fp) if (tp + fp) else 0.0,
        urgent_threshold=thr,
        per_class={k: v for k, v in rep.items() if k in INTENTS},
    )

    if save:
        os.makedirs(MODEL_DIR, exist_ok=True)
        joblib.dump(intent_clf, INTENT_PATH, compress=3)
        joblib.dump({"model": urgent_clf, "threshold": thr}, URGENT_PATH, compress=3)
    return report


# ------------------------------------------------------------------ 추론 --

_cache: dict = {}


def _load():
    """모델을 한 번만 읽어 캐시한다. 없으면 None(규칙 폴백)."""
    if "loaded" in _cache:
        return _cache["intent"], _cache["urgent"]
    intent = urgent = None
    if os.path.exists(INTENT_PATH) and os.path.exists(URGENT_PATH):
        import joblib
        intent = joblib.load(INTENT_PATH)
        urgent = joblib.load(URGENT_PATH)
    _cache.update(loaded=True, intent=intent, urgent=urgent)
    return intent, urgent


def available() -> bool:
    return _load()[0] is not None


@dataclass
class Prediction:
    intent: str
    confidence: float
    urgent: bool
    urgent_score: float
    source: str = "학습모델"


def predict(text: str) -> Prediction | None:
    """학습된 모델로 의도를 예측한다. 모델이 없으면 None → 호출부가 규칙으로 폴백."""
    intent_clf, urgent_pack = _load()
    if intent_clf is None:
        return None
    proba = intent_clf.predict_proba([text])[0]
    idx = int(proba.argmax())
    label = intent_clf.classes_[idx]

    u_model, u_thr = urgent_pack["model"], urgent_pack["threshold"]
    u_score = float(u_model.predict_proba([text])[0][1])
    is_urgent = u_score >= u_thr

    # 긴급은 별도 분류기가 우선한다 — 놓치지 않는 것이 최우선
    if is_urgent:
        label = "긴급"
    return Prediction(intent=str(label), confidence=float(proba[idx]),
                      urgent=is_urgent, urgent_score=u_score)
