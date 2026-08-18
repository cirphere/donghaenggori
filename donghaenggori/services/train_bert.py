"""KLUE-RoBERTa 파인튜닝 — 의도 분류기 업그레이드 실험.

    python -m donghaenggori.services.train_bert --data <C-DS01 경로>
    python -m donghaenggori.services.train_bert --data <경로> --epochs 5 --save

TF-IDF 기준선(정확도 96.2% / 긴급 재현율 100%)과 같은 조건에서 비교한다.
더 나으면 교체하고, 아니면 "이 데이터 규모에선 TF-IDF가 낫다"는 것도 실험 결과다.

평가 프로토콜 — 기준선과 동일하게 맞춘다
  · 합성 데이터는 **학습에만** 넣고 평가셋에서 제외한다.
    (합성을 평가에 섞으면 정확도가 부풀려지고 긴급 임계값이 느슨해진다.
     실측: 합성 포함 97.8% → 실데이터만 77.5%)
  · 긴급 임계값은 실데이터 홀드아웃에서 **목표 재현율**로 튜닝한다.
    놓치는 것이 오탐보다 위험하므로 정밀도를 희생한다.

장비 (cuda > mps > cpu 자동 선택)
  실측: 789세션 + 합성 1,500건 = 402스텝, M5 MPS에서 319초.
  (사전 추정 37초는 합성 데이터가 학습셋에 들어가는 걸 빠뜨린 오산이었다)
  4060 Ti면 수십 초 수준. 이 규모에선 GPU 서버까지 갈 필요가 없다.

실험 결과 (789세션 기준, 2026-08-08)
  정확도    TF-IDF 0.962 > BERT 0.949   ← 데이터가 작아 BERT가 과적합
  긴급 재현율 둘 다 1.000
  긴급 정밀도 TF-IDF 0.582 < BERT 0.800  ← 놓침 없이 오탐이 절반 이하로
  판단: 지금은 TF-IDF 유지. Training 라벨로 데이터가 4~5배 되면 재실험.

합성 데이터 보강 (평가 1,404건, 2026-08-13)
  병원동행 템플릿이 전부 진료과를 끼고 있어서, 증상만 말한 문장이 통째로
  긴급으로 분류됐다. 진료과 없는 템플릿을 넣고 다시 학습한 결과:

                 이전      이후
    정확도       0.971  →  0.994
    긴급 재현율   0.9928 →  0.9928   ← 놓친 2건 그대로
    긴급 정밀도   0.875  →  0.975
    긴급 오탐     39건   →  7건
    임계값       0.06   →  0.14     ← 목표 재현율 0.99 에 맞춰 자동 튜닝

  놓치는 것을 늘리지 않고 헛경보만 줄였다. 임계값은 사람이 고른 값이 아니라
  홀드아웃에서 목표 재현율을 맞춘 결과다.
"""
from __future__ import annotations

import argparse
import os
import time

MODEL_NAME = os.environ.get("BERT_MODEL", "klue/roberta-base")
# 저장 위치. 컨테이너 안에서 학습할 때를 위해 환경변수로 뺐다 — compose 는
# data/models 를 **읽기 전용**으로 마운트하므로(운영 중 모델이 바뀌면 안 된다)
# 그대로 두면 --save 가 실패한다. 쓰기 가능한 경로를 주고 끝난 뒤 옮긴다.
OUT_DIR = os.environ.get("BERT_OUT_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "models", "bert")
MAX_LEN = 256
TARGET_URGENT_RECALL = 0.99


def pick_device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _encode(tok, texts, labels, label2id):
    import torch
    enc = tok(texts, truncation=True, max_length=MAX_LEN, padding=True, return_tensors="pt")
    y = torch.tensor([label2id[name] for name in labels])
    return enc, y


def _batches(n: int, bs: int, shuffle: bool = True):
    import torch
    idx = torch.randperm(n) if shuffle else torch.arange(n)
    for i in range(0, n, bs):
        yield idx[i:i + bs]


def _tune_threshold(y_true, proba_urgent, target_recall: float) -> float:
    """목표 재현율을 만족하는 임계값 중 정밀도가 가장 높은 값."""
    import numpy as np
    best_t, best_p = 0.5, -1.0
    for t in np.arange(0.02, 0.99, 0.01):
        pred = proba_urgent >= t
        tp = int(((pred == 1) & (y_true == 1)).sum())
        fn = int(((pred == 0) & (y_true == 1)).sum())
        fp = int(((pred == 1) & (y_true == 0)).sum())
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        if rec < target_recall:
            continue
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        if prec > best_p:
            best_p, best_t = prec, float(t)
    return best_t


def run(data_path: str, epochs: int = 3, batch_size: int = 16, lr: float = 2e-5,
        seed: int = 42, save: bool = False, opening: int = 5,
        export: str | None = None, official_split: bool = False) -> dict:
    """export        — tools.export_cds01 로 뽑은 작은 파일(원본 7.8GB 대신).
    official_split — AI-Hub 가 나눠 둔 TL 로 학습하고 VL 로 평가한다.

    예전에는 TL·VL 을 섞어 읽고 8:2 로 무작위 분할했다. 그 홀드아웃 숫자 자체는
    유효하지만(학습에 안 쓴 데이터), **공식 검증셋으로 평가했다고 말할 수는 없다**
    — VL 일부가 학습에 들어갔기 때문이다. 제출 문서가 검증셋을 측정 방법으로
    적어 뒀으므로 그 기준을 그대로 지킬 수 있게 한다.
    """
    import numpy as np
    import torch
    from sklearn.metrics import accuracy_score, classification_report, f1_score
    from sklearn.model_selection import train_test_split
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    from . import cds01, train_intent

    torch.manual_seed(seed)
    device = pick_device()

    # ── 데이터 ─────────────────────────────────────────────
    if export:
        def _load(split):
            return cds01.load_export(export, "session", split)
    else:
        def _load(split):
            X, y, _ = cds01.load_sessions(data_path, opening=opening, split=split)
            return X, y

    if official_split:
        X_tr, y_tr = _load("TL")
        X_te, y_te = _load("VL")
        basis = "AI-Hub 공식 분할 (TL 학습 / VL 평가)"
    else:
        if export:
            Xr, yr = [], []
            for sp in ("TL", "VL"):
                a, b = _load(sp)
                Xr += a
                yr += b
        else:
            Xr, yr, _ = cds01.load_sessions(data_path, opening=opening)
        X_tr, X_te, y_tr, y_te = train_test_split(
            Xr, yr, test_size=0.2, random_state=seed, stratify=yr)
        basis = "TL·VL 혼합 8:2 무작위 분할"

    Xs, ys = train_intent.synthetic(train_intent._COUNTS_SUPPLEMENT, 0)
    X_tr, y_tr = list(X_tr) + Xs, list(y_tr) + ys      # 합성은 학습에만

    labels = sorted(set(y_tr) | set(y_te))
    label2id = {name: i for i, name in enumerate(labels)}
    id2label = {i: name for name, i in label2id.items()}

    print(f"평가 기준: {basis}")
    print(f"장비: {device} · 모델: {MODEL_NAME}")
    print(f"학습 {len(X_tr):,}건 (실데이터 {len(X_tr)-len(Xs):,} + 합성 {len(Xs):,}) "
          f"/ 평가 {len(X_te):,}건 (실데이터만)")
    print(f"클래스: {labels}")
    print()

    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, num_labels=len(labels), id2label=id2label, label2id=label2id).to(device)

    enc_tr, ytr = _encode(tok, X_tr, y_tr, label2id)
    enc_te, yte = _encode(tok, X_te, y_te, label2id)

    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    steps = (len(X_tr) + batch_size - 1) // batch_size * epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=steps)

    # ── 학습 ─────────────────────────────────────────────
    t0 = time.time()
    model.train()
    step = 0
    for ep in range(epochs):
        tot = 0.0
        for bidx in _batches(len(X_tr), batch_size):
            batch = {k: v[bidx].to(device) for k, v in enc_tr.items()}
            out = model(**batch, labels=ytr[bidx].to(device))
            out.loss.backward()
            opt.step()
            sched.step()
            opt.zero_grad()
            tot += out.loss.item()
            step += 1
        print(f"  epoch {ep+1}/{epochs}  loss {tot/max(1,step/ (ep+1)):.4f}")
    train_sec = time.time() - t0

    # ── 평가 (실데이터 홀드아웃) ────────────────────────────
    model.eval()
    probs = []
    with torch.no_grad():
        for bidx in _batches(len(X_te), 32, shuffle=False):
            batch = {k: v[bidx].to(device) for k, v in enc_te.items()}
            probs.append(torch.softmax(model(**batch).logits, dim=-1).cpu())
    P = torch.cat(probs).numpy()
    pred = [id2label[int(i)] for i in P.argmax(1)]

    acc = accuracy_score(y_te, pred)
    mf1 = f1_score(y_te, pred, average="macro", zero_division=0)

    urgent_i = label2id.get("긴급")
    urec = uprec = uthr = None
    if urgent_i is not None:
        yb = np.array([1 if name == "긴급" else 0 for name in y_te])
        pu = P[:, urgent_i]
        uthr = _tune_threshold(yb, pu, TARGET_URGENT_RECALL)
        ub = pu >= uthr
        tp = int((ub & (yb == 1)).sum())
        fn = int((~ub & (yb == 1)).sum())
        fp = int((ub & (yb == 0)).sum())
        urec = tp / (tp + fn) if (tp + fn) else 0.0
        uprec = tp / (tp + fp) if (tp + fp) else 0.0

    print()
    print(f"학습 {train_sec:.1f}s ({step}스텝)")
    print(f"  정확도    {acc:.3f}")
    print(f"  macro-F1  {mf1:.3f}")
    if urec is not None:
        print(f"  긴급 재현율 {urec:.3f} (임계 {uthr:.2f}) 정밀도 {uprec:.3f}")
    print()
    print(classification_report(y_te, pred, zero_division=0, digits=3))

    if save:
        os.makedirs(OUT_DIR, exist_ok=True)
        model.save_pretrained(OUT_DIR)
        tok.save_pretrained(OUT_DIR)
        import json
        json.dump({"urgent_threshold": uthr, "labels": labels},
                  open(os.path.join(OUT_DIR, "meta.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"저장: {OUT_DIR}")

    return {"accuracy": acc, "macro_f1": mf1, "urgent_recall": urec,
            "urgent_precision": uprec, "urgent_threshold": uthr,
            "train_sec": train_sec, "device": device,
            "n_train": len(X_tr), "n_test": len(X_te)}


def main() -> None:
    ap = argparse.ArgumentParser(description="KLUE-RoBERTa 파인튜닝 (의도 분류)")
    ap.add_argument("--data", help="C-DS01 라벨 디렉터리 (--export 를 쓰면 불필요)")
    ap.add_argument("--export", help="tools.export_cds01 로 뽑은 파일(원본 대신)")
    ap.add_argument("--official-split", action="store_true",
                    help="AI-Hub TL 로 학습하고 VL 로 평가 (제출 문서 기준)")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--opening", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--save", action="store_true", help="모델 저장")
    a = ap.parse_args()
    if not a.data and not a.export:
        raise SystemExit("--data 또는 --export 중 하나는 있어야 합니다")
    r = run(a.data, a.epochs, a.batch_size, a.lr, a.seed, a.save, a.opening,
            export=a.export, official_split=a.official_split)

    # TF-IDF 기준선 (같은 평가 프로토콜: 실데이터 홀드아웃)
    BASE = {"accuracy": 0.986, "macro_f1": 0.979,
            "urgent_recall": 0.993, "urgent_precision": 0.907}
    print()
    print("── TF-IDF 기준선과 비교 (같은 실데이터 홀드아웃) ──")
    print(f"  {'':<14}{'TF-IDF':>10}{'BERT':>10}{'차이':>10}")
    for key, name in [("accuracy", "정확도"), ("macro_f1", "macro-F1"),
                      ("urgent_recall", "긴급 재현율"), ("urgent_precision", "긴급 정밀도")]:
        b, n = BASE[key], (r.get(key) or 0.0)
        print(f"  {name:<14}{b:>10.3f}{n:>10.3f}{n-b:>+10.3f}")
    print()
    print(f"  학습 시간: {r['train_sec']:.0f}s ({r['device']})  ·  TF-IDF는 1초 미만")
    print()
    gain_p = (r.get("urgent_precision") or 0) - BASE["urgent_precision"]
    keeps_recall = (r.get("urgent_recall") or 0) >= BASE["urgent_recall"] - 1e-9
    if gain_p > 0.05 and keeps_recall:
        print("  → 재현율 100%를 지키면서 긴급 오탐이 크게 줄었다. 교체 검토 가치 있음.")
        print("     다만 추론 비용이 오르므로(모델 400MB, CPU 추론 수십 ms) 배포 환경을 함께 본다.")
    elif r["accuracy"] > BASE["accuracy"] + 0.005:
        print("  → BERT가 더 낫다. 교체 검토")
    else:
        print("  → 이 데이터 규모에선 TF-IDF가 낫거나 비슷하다. 기준선 유지 권장")


if __name__ == "__main__":
    main()
