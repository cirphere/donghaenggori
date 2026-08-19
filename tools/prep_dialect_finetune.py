"""방언 데이터 → Whisper 파인튜닝 학습셋.

    python -m tools.prep_dialect_finetune \
        --audio-zip VS_jeonla_qa.zip --label-zip VL_jeonla_qa.zip \
        --out train --exclude-manifest eval_manifest.csv

AI Hub 중·노년층 방언 데이터(139-2)를 그대로는 못 쓴다. 세 가지를 맞춰야 한다.

**① 30초를 넘기지 않는다.** Whisper 는 30초 창으로 학습한다. 이 데이터는
75% 가 34초를 넘어서(중앙값 19초, 최대 141초) 그냥 넣으면 뒤가 잘린 채
학습된다 — 오디오는 잘리는데 정답 텍스트는 온전해서, 모델이 "안 들린 말도
지어내라" 를 배운다. 라벨에 문장별 시작·끝 시각이 있으므로 **문장 경계로**
자른다. 문장 중간에서 자르면 그 조각의 정답이 어디까지인지 알 수 없다.

**② 8kHz 전화 음질로 떨어뜨린다.** 배포 입력은 ClawOps 전화 녹음(8kHz
협대역)인데 이 데이터는 16kHz 광대역이다. 광대역으로 학습해 8kHz 에 붙이면
이득의 상당 부분이 넘어오지 않고, 실제로 보지 않을 채널에 적응해 오히려
나빠질 수 있다. tools/stt_eval.py 가 측정에 쓰는 것과 **같은 변환**을 쓴다 —
평가와 학습의 채널이 어긋나면 개선을 잴 수 없다.

**③ 정답은 방언형(dialect)으로 적는다.** 표준형(standard)으로 학습하면
방언을 표준어로 바꿔 적는 모델이 된다. voice_samples/README 가 사람에게
라벨을 고치라고 할 때 건 원칙과 같다 — "들린 대로 적는다. 방언을 표준어로
바꾸면 방언을 못 배운다."

평가셋으로 쓰는 파일은 --exclude-manifest 로 빼야 한다. 같은 화자의 다른
발화까지 빼지는 않는다(화자 단위 분리는 --exclude-speakers 로 켠다) —
방언 적응은 화자 특성이 아니라 지역 말씨를 배우는 것이 목적이라 기본은
발화 단위로 나눈다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import zipfile

MAX_CHUNK_SECONDS = 30.0
MIN_CHUNK_SECONDS = 0.6          # 이보다 짧으면 학습에 쓸 것이 없다


def _hhmmss(v: str) -> float:
    """'00:01:23.456' → 83.456"""
    h, m, s = v.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def sentences(label: dict) -> list[tuple[float, float, str]]:
    """(시작, 끝, 방언 전사) 목록. 시각이나 전사가 없는 것은 버린다."""
    tr = label.get("transcription") or {}
    rows = tr.get("sentences")
    if not isinstance(rows, list) or not rows:
        rows = tr.get("segments") or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        text = (r.get("dialect") or "").strip()
        start, end = r.get("startTime"), r.get("endTime")
        if not text or not isinstance(start, str) or not isinstance(end, str):
            continue
        try:
            a, b = _hhmmss(start), _hhmmss(end)
        except (ValueError, AttributeError):
            continue
        if b > a:
            out.append((a, b, text))
    return sorted(out)


def chunk(sents: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """문장을 30초 이하 덩어리로 묶는다.

    **문장을 쪼개지 않는다.** 한 문장이 30초를 넘으면 그 문장은 통째로 버린다 —
    중간에서 자르면 그 조각의 정답이 무엇인지 말할 수 없다.
    """
    out: list[tuple[float, float, str]] = []
    cur: list[tuple[float, float, str]] = []
    for s in sents:
        if s[1] - s[0] > MAX_CHUNK_SECONDS:
            if cur:
                out.append((cur[0][0], cur[-1][1], " ".join(t for _, _, t in cur)))
                cur = []
            continue
        if cur and s[1] - cur[0][0] > MAX_CHUNK_SECONDS:
            out.append((cur[0][0], cur[-1][1], " ".join(t for _, _, t in cur)))
            cur = []
        cur.append(s)
    if cur:
        out.append((cur[0][0], cur[-1][1], " ".join(t for _, _, t in cur)))
    return [c for c in out if c[1] - c[0] >= MIN_CHUNK_SECONDS]


def to_telephony_segment(src: str, start: float, end: float, dst: str) -> bool:
    """구간을 잘라 8kHz μ-law 를 거쳐 16kHz 로 되올린다.

    stt_eval.to_telephony 와 같은 경로다. 한 번에 못 하고 두 단계로 나누는 것은
    μ-law 로 실제 인코딩했다 디코딩해야 전화망에서 잃는 것이 재현되기 때문이다.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav") as mid:
        one = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
               "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", mid.name]
        two = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", mid.name, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst]
        try:
            subprocess.run(one, check=True, capture_output=True)
            subprocess.run(two, check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="방언 데이터 → Whisper 학습셋")
    ap.add_argument("--audio-zip", required=True)
    ap.add_argument("--label-zip", required=True)
    ap.add_argument("--out", required=True, help="출력 디렉토리")
    ap.add_argument("--exclude-manifest", help="평가셋 manifest.csv (stem 열) — 학습에서 뺀다")
    ap.add_argument("--exclude-speakers", action="store_true",
                    help="평가셋에 등장한 화자의 발화를 전부 뺀다(화자 단위 분리)")
    ap.add_argument("--limit", type=int, default=0, help="원본 파일 수 상한(0=전부)")
    ap.add_argument("--no-telephony", action="store_true", help="전화 열화 없이 원본 그대로")
    args = ap.parse_args()

    za = zipfile.ZipFile(args.audio_zip)
    zl = zipfile.ZipFile(args.label_zip)
    aud = {os.path.splitext(os.path.basename(n))[0]: n
           for n in za.namelist() if n.lower().endswith(".wav")}
    lab = {os.path.splitext(os.path.basename(n))[0]: n
           for n in zl.namelist() if n.lower().endswith(".json")}

    stems = sorted(set(aud) & set(lab))
    drop: set[str] = set()
    if args.exclude_manifest:
        with open(args.exclude_manifest, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        drop = {r["stem"] for r in rows}
        if args.exclude_speakers:
            spk = set()
            for s in drop:
                d = json.loads(zl.read(lab[s]).decode("utf-8"))
                sp = (d.get("speaker") or [{}])[0]
                if sp.get("speakerId"):
                    spk.add(sp["speakerId"])
            for s in list(stems):
                d = json.loads(zl.read(lab[s]).decode("utf-8"))
                sp = (d.get("speaker") or [{}])[0]
                if sp.get("speakerId") in spk:
                    drop.add(s)
        stems = [s for s in stems if s not in drop]
    if args.limit:
        stems = stems[:args.limit]

    audio_dir = os.path.join(args.out, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    manifest = os.path.join(args.out, "manifest.jsonl")

    kept = skipped = 0
    total = 0.0
    with open(manifest, "w", encoding="utf-8") as mf, \
            tempfile.TemporaryDirectory() as tmp:
        for i, stem in enumerate(stems, 1):
            label = json.loads(zl.read(lab[stem]).decode("utf-8"))
            parts = chunk(sentences(label))
            if not parts:
                skipped += 1
                continue
            src = os.path.join(tmp, f"{stem}.wav")
            with open(src, "wb") as f:
                f.write(za.read(aud[stem]))
            sp = (label.get("speaker") or [{}])[0]
            for k, (a, b, text) in enumerate(parts):
                dst = os.path.join(audio_dir, f"{stem}__{k:02d}.wav")
                if args.no_telephony:
                    ok = subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                         "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", src,
                         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst],
                        capture_output=True).returncode == 0
                else:
                    ok = to_telephony_segment(src, a, b, dst)
                if not ok:
                    skipped += 1
                    continue
                mf.write(json.dumps({
                    "audio": os.path.relpath(dst, args.out),
                    "text": text,
                    "duration": round(b - a, 3),
                    "speaker": sp.get("speakerId"),
                    "birthYear": sp.get("birthYear"),
                }, ensure_ascii=False) + "\n")
                kept += 1
                total += b - a
            os.unlink(src)
            if i % 200 == 0:
                print(f"  {i}/{len(stems)} 원본 · 조각 {kept}개 · {total/3600:.1f}h",
                      file=sys.stderr, flush=True)

    print(f"\n원본 {len(stems)}개 → 조각 {kept}개 ({total/3600:.2f} 시간)")
    print(f"제외 {len(drop)}개(평가셋) · 건너뜀 {skipped}")
    print(f"manifest: {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
