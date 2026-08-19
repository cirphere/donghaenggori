"""업무 기록 → 학습 라벨 추출.

    python -m tools.export_labels                 # 요약만 본다
    python -m tools.export_labels --out labels/   # jsonl 로 뽑는다

**별도 라벨링 작업이 없다.** 복지사가 확인 전화를 걸고 '확인함' 을 누르는
것, 사후기록을 승인하는 것 — 어차피 하는 업무다. 그 흔적이 그대로 (입력,
AI 답, 사람이 고친 정답) 삼중쌍이 된다. 게다가 확인하지 않으면 확정이
409 로 막히므로, 라벨 생산이 절차로 강제된다.

세 곳에서 나온다.

    접수 verify   원문 + AI 슬롯값 + 사람이 확인한 값
    사후기록      매니저 메모 + AI 초안(draft_json) + 승인된 값
    통화 표본     음성 파일 + 전사 초안 (사람이 들으며 고쳐야 라벨)

**고친 것과 확인만 한 것을 구분해 센다.** AI 가 맞혔는데 확인만 한 것과
틀린 것을 고친 것은 완전히 다른 사건이다. 뒤쪽만이 모델이 배울 것이고,
그 비율이 곧 지금 무엇을 못 하는지를 말해 준다.

개인정보: 뽑은 파일에는 발화 원문과 병원·진료과가 들어간다. 저장소에
커밋하지 않는다(.gitignore). 학습에 쓸 때 이름·연락처가 섞이지 않았는지
사람이 한 번 훑을 것.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from donghaenggori.core import db  # noqa: E402

# "dept: 정형외과 → 안과" (고침) / "dept=안과" (확인만)
_CHANGED = re.compile(r"^(?P<field>\w+):\s*(?P<was>.+?)\s*→\s*(?P<now>.+)$")
_CONFIRMED = re.compile(r"^(?P<field>\w+)=(?P<now>.+)$")

# 사후기록에서 AI 초안과 승인값을 비교할 항목.
_POST_FIELDS = ("treatment", "next_visit", "pharmacy", "cautions", "guardian_msg")


def _intake_labels(conn) -> list[dict]:
    """접수 verify — 원문과 사람이 확인한 슬롯 값."""
    out = []
    rows = conn.execute(
        "SELECT a.target_id, a.at, a.actor, a.detail, i.raw_utterance, i.channel "
        "FROM audit_log a JOIN intakes i ON i.id = CAST(a.target_id AS INTEGER) "
        "WHERE a.action='항목확인' ORDER BY a.id").fetchall()
    for r in rows:
        detail = (r["detail"] or "").strip()
        m = _CHANGED.match(detail)
        changed = m is not None
        if not m:
            m = _CONFIRMED.match(detail)
        if not m:
            continue
        out.append({
            "source": "intake_verify",
            "at": r["at"],
            "input": r["raw_utterance"],
            "channel": r["channel"],
            "field": m.group("field"),
            "ai": m.groupdict().get("was"),      # 확인만 한 경우 None
            "label": m.group("now").strip(),
            "changed": changed,
        })
    return out


def _post_labels(conn) -> list[dict]:
    """사후기록 — 매니저 메모와 승인된 항목. AI 초안이 그대로 남아 있다."""
    out = []
    rows = conn.execute(
        "SELECT id, at_created, memo_raw, draft_json, "
        + ", ".join(_POST_FIELDS)
        + " FROM (SELECT *, created_at AS at_created FROM post_records) "
          "WHERE approved=1").fetchall()
    for r in rows:
        try:
            draft = json.loads(r["draft_json"]) if r["draft_json"] else {}
        except (TypeError, ValueError):
            draft = {}
        for f in _POST_FIELDS:
            final = (r[f] or "").strip()
            if not final:
                continue
            ai = (draft.get(f) or "").strip() or None
            out.append({
                "source": "post_record",
                "at": r["at_created"],
                "input": r["memo_raw"],
                "field": f,
                "ai": ai,
                "label": final,
                "changed": bool(ai and ai != final),
            })
    return out


def _voice_samples(sample_dir: str) -> list[dict]:
    """통화 표본 — 음성과 전사 초안. **전사는 정답이 아니다**(사람이 고쳐야 한다)."""
    out = []
    if not os.path.isdir(sample_dir):
        return out
    for name in sorted(os.listdir(sample_dir)):
        if not name.endswith(".wav"):
            continue
        txt = os.path.join(sample_dir, name[:-4] + ".txt")
        if not os.path.exists(txt):
            continue
        with open(txt, encoding="utf-8") as f:
            draft = f.read().strip()
        out.append({
            "source": "voice_sample",
            "at": name[:15],
            "audio": os.path.join(sample_dir, name),
            "field": "transcript",
            "ai": draft,
            "label": None,           # 사람이 들으며 채워야 한다
            "changed": None,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="jsonl 을 쓸 디렉터리. 없으면 요약만 출력한다.")
    ap.add_argument("--samples", default=None, help="통화 표본 디렉터리")
    args = ap.parse_args()

    db.init_db()
    conn = db.get_conn()
    try:
        rows = _intake_labels(conn) + _post_labels(conn)
    finally:
        conn.close()
    sample_dir = args.samples or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "donghaenggori", "data", "voice_samples")
    rows += _voice_samples(sample_dir)

    print("업무 기록에서 뽑은 학습 라벨")
    print("=" * 66)
    if not rows:
        print("  아직 없습니다. 접수를 확인·확정하거나 사후기록을 승인하면 쌓입니다.")
        return 0

    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)

    label_names = {"intake_verify": "접수 확인", "post_record": "사후기록 승인",
                   "voice_sample": "통화 표본"}
    for src, items in by_source.items():
        changed = sum(1 for i in items if i["changed"])
        confirmed = sum(1 for i in items if i["changed"] is False)
        pending = sum(1 for i in items if i["changed"] is None)
        line = f"  {label_names.get(src, src):14} {len(items):3}건"
        if pending:
            line += f"   (사람이 전사를 고쳐야 라벨이 됨 {pending}건)"
        else:
            line += f"   고침 {changed} · 확인만 {confirmed}"
        print(line)
        fields: dict[str, int] = {}
        for i in items:
            fields[i["field"]] = fields.get(i["field"], 0) + 1
        print(f"  {'':14} 항목: " + ", ".join(f"{k} {v}" for k, v in sorted(fields.items())))

    total_changed = sum(1 for r in rows if r["changed"])
    usable = [r for r in rows if r["label"]]
    print("-" * 66)
    print(f"  라벨 {len(usable)}건 · 그중 AI 가 틀려서 고친 것 {total_changed}건")
    print()
    print("  **고친 것이 배울 거리다.** 확인만 한 것은 AI 가 이미 맞힌 것이고,")
    print("  고친 것은 지금 못 하는 것이다 — 그 비율이 다음에 뭘 볼지 알려준다.")

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        stamp = datetime.date.today().isoformat()
        path = os.path.join(args.out, f"labels-{stamp}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print()
        print(f"  → {path} ({len(rows)}줄)")
        print("  발화 원문과 병원명이 들어 있다. 커밋하지 말 것.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
