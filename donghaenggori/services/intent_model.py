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
# 모델 위치. 컨테이너 안에서 학습할 때를 위해 환경변수로 뺐다 — compose 는
# 이 디렉터리를 읽기 전용으로 마운트하므로 그대로 두면 학습 저장이 실패한다.
MODEL_DIR = os.environ.get("MODEL_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "models")
INTENT_PATH = os.path.join(MODEL_DIR, "intent_clf.pkl")
URGENT_PATH = os.path.join(MODEL_DIR, "urgent_clf.pkl")

# 긴급 감지 목표 재현율.
# 놓치는 것이 오탐보다 훨씬 위험하므로 재현율을 우선한다.
# 전체 데이터(7,019세션) 기준 트레이드오프 실측:
#   0.97 → 놓침 8건 / 오탐 10건
#   0.99 → 놓침 2건 / 오탐 25건   ← 채택
#   1.00 → 놓침 0건 / 오탐 57건 (홀드아웃 한 번에 맞춘 임계값이라 과적합 위험)
# 규칙 사전이 병렬로 동작해 명백한 표현("가슴이 아파", "숨이 차")은 별도로 잡는다.
TARGET_URGENT_RECALL = 0.99


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
          seed: int = 42, save: bool = True,
          aux_texts: list[str] | None = None,
          aux_labels: list[str] | None = None) -> TrainReport:
    """의도 분류기 + 긴급 이진분류기를 학습한다.

    aux_*: 학습에만 쓰고 평가에서는 제외할 보조 데이터(합성 문장 등).
        합성 데이터는 템플릿이라 거의 100% 맞힌다. 평가셋에 섞으면 성능이
        부풀려지고, 특히 긴급 임계값이 실제보다 느슨하게 잡힌다.
        (실측: 합성 포함 평가 재현율 0.978 → 실데이터만 평가 0.775)
        따라서 임계값 튜닝과 성능 보고는 **실데이터 홀드아웃**으로만 한다.
    """
    import joblib
    import numpy as np
    from sklearn.metrics import accuracy_score, classification_report, f1_score
    from sklearn.model_selection import train_test_split

    X_tr, X_te, y_tr, y_te = train_test_split(
        texts, labels, test_size=test_size, random_state=seed, stratify=labels)
    if aux_texts:
        X_tr = X_tr + list(aux_texts)
        y_tr = y_tr + list(aux_labels or [])

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
    tp = int((ub & (yb_te == 1)).sum())
    fn = int((~ub & (yb_te == 1)).sum())
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


# ─────────────────────────────── BERT 경로 (있으면 우선) ──
# 3단 폴백: KLUE-RoBERTa → TF-IDF → 규칙 사전
# 무거운 의존성이 없거나 모델이 없어도 서비스가 계속 돌아가야 한다.
BERT_DIR = os.path.join(MODEL_DIR, "bert")
_bert: dict = {}


def _load_bert():
    """BERT를 적재한다. 실패해도 서비스는 계속 도는 대신, **이유를 남긴다**.

    조용히 TF-IDF로 내려가면 배포에서 이걸 눈치채지 못한다. 실제로 컨테이너
    사용자(UID 10001)가 model.safetensors(권한 600)를 못 읽어 폴백한 적이 있고,
    /api/status 에 'BERT'가 아니라 'TF-IDF'로 뜨는 것 말고는 단서가 없었다.
    """
    if "tried" in _bert:
        return _bert.get("model")
    _bert["tried"] = True
    meta_path = os.path.join(BERT_DIR, "meta.json")
    if not os.path.exists(meta_path):
        _bert["error"] = f"모델 없음: {meta_path}"
        return None
    weights = os.path.join(BERT_DIR, "model.safetensors")
    if os.path.exists(weights) and not os.access(weights, os.R_OK):
        # 가장 흔한 배포 사고 — 바인드마운트한 파일을 컨테이너 사용자가 못 읽는다
        _bert["error"] = (f"가중치를 읽을 수 없음(권한): {weights} — "
                          f"호스트에서 chmod a+r 하세요")
        return None
    try:
        import json

        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        meta = json.load(open(meta_path, encoding="utf-8"))
        tok = AutoTokenizer.from_pretrained(BERT_DIR)
        model = AutoModelForSequenceClassification.from_pretrained(BERT_DIR).eval()
        _bert.update(model=model, tok=tok, torch=torch,
                     thr=meta.get("urgent_threshold", 0.5),
                     id2label=model.config.id2label)
        return model
    except Exception as e:
        _bert["error"] = f"{type(e).__name__}: {e}"
        return None


def bert_available() -> bool:
    return _load_bert() is not None


def bert_error() -> str | None:
    """BERT를 못 쓰는 이유. 정상 적재됐으면 None."""
    _load_bert()
    return _bert.get("error")


def _predict_bert(text: str) -> Prediction | None:
    if _load_bert() is None:
        return None
    torch, tok, model = _bert["torch"], _bert["tok"], _bert["model"]
    with torch.no_grad():
        enc = tok([text], truncation=True, max_length=256, return_tensors="pt")
        p = torch.softmax(model(**enc).logits, dim=-1)[0]
    idx = int(p.argmax())
    label = _bert["id2label"][idx]
    urgent_idx = next((i for i, name in _bert["id2label"].items() if name == "긴급"), None)
    u = float(p[urgent_idx]) if urgent_idx is not None else 0.0
    is_urgent = u >= _bert["thr"]
    return Prediction(intent="긴급" if is_urgent else str(label),
                      confidence=float(p[idx]), urgent=is_urgent,
                      urgent_score=u, source="BERT")


def predict(text: str) -> Prediction | None:
    """의도를 예측한다. BERT → TF-IDF → None(호출부가 규칙으로 폴백)."""
    b = _predict_bert(text)
    if b is not None:
        return b

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
                      urgent=is_urgent, urgent_score=u_score, source="TF-IDF")
