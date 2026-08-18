"""병원 후보 Top-3 적중률 — 제출 문서(파일1) 4-2 지표.

문서에 적힌 정의를 그대로 잰다.
    지표      정답 병원이 후보 3개 안에 포함
    측정 방법  가상 이력 60건 기반
    본선 목표  0.85 이상

**정답은 지어내지 않는다.** 어르신이 실제로 방문한 이력 한 건 한 건이 정답이다.
각 방문에 대해 "그 방문 직전 시점에 시스템이 무엇을 후보로 냈겠는가"를 물어,
실제 간 병원이 상위 3개 안에 있었는지 본다.

미래를 안 보게 막는 것이 이 측정의 전부다 — 채점하려는 방문 자신과 그 이후
방문은 이력에서 뺀다. 안 빼면 정답을 이력으로 주고 정답을 맞히랬 격이라
적중률이 1.0 으로 나오고, 그 숫자는 아무 의미가 없다.

실행:  PYTHONPATH=. python tests/metrics_hospital_top3.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from donghaenggori.core import hospital  # noqa: E402

TARGET = 0.85          # 파일1 4-2 본선 목표
TOP_N = 3
_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "donghaenggori", "data", "care_profiles.json")


def load_profiles() -> dict:
    with open(_DATA, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def backtest() -> dict:
    """방문 한 건을 한 번씩 채점한다(prequential)."""
    rows = []
    for phone, prof in load_profiles().items():
        history = sorted(prof.get("history") or [], key=lambda h: h.get("date") or "")
        for h in history:
            truth, date_s = h.get("hospital"), h.get("date")
            if not truth or not date_s:
                continue
            try:
                today = datetime.date.fromisoformat(date_s)
            except ValueError:
                continue

            # 그 방문 이전 것만 남긴다. 같은 날 방문도 뺀다 — 접수 시점에는
            # 아직 기록되지 않았을 정보다.
            prior = [x for x in history if (x.get("date") or "") < date_s]
            before = {**prof, "history": prior}

            r = hospital.suggest(before, dept=h.get("dept"), today=today)
            ranked = [c.get("hospital") for c in (r.candidates or [])]
            rows.append({
                "phone": phone, "name": prof.get("name"), "date": date_s,
                "dept": h.get("dept"), "truth": truth,
                "cold": not prior,                      # 이력이 하나도 없던 첫 방문
                "top1": bool(ranked[:1] and ranked[0] == truth),
                "topn": truth in ranked[:TOP_N],
                "status": r.status,
                "n_cand": len(ranked),
            })
    return {"rows": rows}


def _rate(rows: list[dict], key: str) -> tuple[int, int, float]:
    hit = sum(1 for r in rows if r[key])
    return hit, len(rows), (hit / len(rows) if rows else 0.0)


def main() -> int:
    rows = backtest()["rows"]
    warm = [r for r in rows if not r["cold"]]
    cold = [r for r in rows if r["cold"]]

    print("=" * 74)
    print("  병원 후보 Top-3 적중률 — 파일1 4-2 지표")
    print("=" * 74)
    print(f"  채점 대상 : 가상 이력 {len(rows)}건 (프로필 {len({r['phone'] for r in rows})}명)")
    print("  방법      : 방문 직전 시점 재현 — 그 방문과 이후 이력을 뺀 상태로 후보 생성")
    print()

    h, n, all_rate = _rate(rows, "topn")
    wh, wn, warm_rate = _rate(warm, "topn")
    t1h, t1n, top1_rate = _rate(rows, "top1")

    print(f"  전체 기준          Top-{TOP_N} {h}/{n} = {all_rate:.3f}")
    print(f"  이력 있는 접수만   Top-{TOP_N} {wh}/{wn} = {warm_rate:.3f}")
    print(f"  (참고) 전체 Top-1  {t1h}/{t1n} = {top1_rate:.3f}")
    print()
    print(f"  첫 방문(이력 0건) {len(cold)}건은 후보를 낼 근거가 없어 '확인 필요'로 나간다.")
    print("  이 건들은 전체 기준에서 미적중으로 잡힌다 — 빼고 재면 숫자가 좋아지므로")
    print("  두 값을 함께 적는다.")
    print()

    print("  " + "-" * 70)
    print(f"  {'날짜':<12}{'대상자':<8}{'진료과':<8}{'상태':<10}{'후보':>4}  적중")
    print("  " + "-" * 70)
    for r in rows:
        mark = "O" if r["topn"] else ("-" if r["cold"] else "X")
        print(f"  {r['date']:<12}{(r['name'] or ''):<8}{(r['dept'] or ''):<8}"
              f"{r['status']:<10}{r['n_cand']:>4}  {mark}")
    print("  " + "-" * 70)
    print("  O 적중 · X 미적중 · - 첫 방문(근거 없음)")
    print()

    # 파일1 4-2 의 "안전 지표 우선 원칙" — 적중률보다 이쪽이 상위 지표다.
    # 틀렸을 때 시스템이 무엇이라고 말했는지가 병원동행에서는 더 중요하다.
    missed = [r for r in rows if not r["topn"]]
    confident_wrong = [r for r in missed if r["status"] in ("확인됨", "추정")]
    print("  " + "-" * 70)
    print("  못 맞힌 건을 시스템이 어떻게 표시했나 (안전 지표 우선 원칙)")
    print("  " + "-" * 70)
    for st in ("확인됨", "추정", "확인 필요"):
        n_st = sum(1 for r in missed if r["status"] == st)
        note = "  ← 틀린 답을 확신 있게 냄" if st in ("확인됨", "추정") and n_st else ""
        print(f"    {st:<10} {n_st:>3}건{note}")
    print()
    print(f"  못 맞힌 {len(missed)}건 중 확신 있게 틀린 것: {len(confident_wrong)}건")
    if not confident_wrong:
        print("  → 모르는 것을 아는 척한 경우가 없다. 전부 사회복지사에게 넘겼다.")
    print()

    # 후보 수를 밝히지 않으면 'Top-3' 라는 이름이 실제보다 후하게 들린다.
    n_multi = sum(1 for r in warm if r["n_cand"] >= 2)
    print("  " + "-" * 70)
    print(f"  후보 개수 — 이력 있는 {len(warm)}건 중 후보가 2개 이상인 건: {n_multi}건")
    if not n_multi:
        print("  → 진료과로 압축하면 한 분당 병원이 하나로 좁혀진다. 이 데이터에서")
        print("     Top-3 는 정의상 Top-1 과 같다. 숫자를 인용할 때 함께 밝힐 것.")
    print()

    ok = warm_rate >= TARGET
    print("=" * 74)
    print(f"  본선 목표 {TARGET:.2f} 대비 — 이력 있는 접수 기준 {warm_rate:.3f} "
          f"{'달성' if ok else '미달'}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
