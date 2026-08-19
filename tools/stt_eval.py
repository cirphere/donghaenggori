"""STT 성능 측정 — 지금 배포된 그 설정으로, 전화 조건에서.

    python -m tools.stt_eval --data /path/to/방언데이터 --limit 200
    python -m tools.stt_eval --data ... --no-telephony      # 광대역 원본으로도
    python -m tools.stt_eval --data ... --errors errors.tsv # 틀린 것만 뽑는다

**왜 필요한가.** 우리는 STT 정확도를 한 번도 재본 적이 없다. 슬롯 정확도
(tests/metrics_slot_accuracy.py)는 텍스트를 입력으로 받으므로 "전사가
완벽했다면" 의 숫자다. 음성부터 카드까지가 통째로 미측정 구간이었다.

**두 가지를 조심했다.**

① 배포와 같은 것을 잰다. faster-whisper 를 직접 부르지 않고
   services.stt.transcribe() 를 그대로 쓴다. hotwords·VAD·DOMAIN_PROMPT·
   후보정(_FIXUPS)까지 포함해야 "지금 우리 STT" 를 잰 것이 된다. 모델만
   따로 부르면 실제보다 나쁘게 나오고, 그 숫자로 판단하면 틀린 결정을 한다.

② 전화 조건을 재현한다. 우리 입력은 8kHz 전화 음성인데 공개 방언 데이터는
   광대역 녹음이다. 그대로 재면 실제보다 좋게 나온다. 8kHz + μ-law 로 한 번
   내렸다 16kHz 로 되올린다 — 잘려나간 대역은 돌아오지 않으므로 전화
   대역제한이 남은 채로 전사된다(Whisper 입력이 16kHz 여야 해서 되올린다).

**한국어는 CER 이 주 지표다.** 띄어쓰기가 흔들려도 슬롯 추출에는 영향이
거의 없는데 WER 은 그걸 통째로 오답으로 센다. 그래서 공백을 뺀 CER 을
먼저 본다. WER 도 같이 내지만 참고용이다.

**이 스크립트의 진짜 산출물은 숫자가 아니라 --errors 파일이다.** 무엇이
어떻게 틀렸는지가 나오면 hotwords 와 _FIXUPS 에 넣을 것이 정해진다. 그건
파인튜닝 없이 몇 분 만에 반영되고 되돌리기도 쉽다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

AUDIO_EXT = (".wav", ".flac", ".mp3", ".m4a", ".pcm")


# ------------------------------------------------------------- 데이터 찾기 --

def _stitched_transcript(data: object) -> str | None:
    """전사문이 조각으로 쪼개져 있는 스키마를 이어 붙인다.

    AI Hub 중·노년층 방언 데이터(139-2)가 이렇다. 전사문을 담은 **문자열
    필드가 아예 없고**, transcription.sentences[] / segments[] 안에
    어절·문장 단위로 흩어져 있다.

        transcription.segments[] = [{"dialect": "하믄", "standard": "하면"}, ...]

    **폴백(가장 긴 문자열)에 맡기면 안 된다.** 이 파일에서 제일 긴 문자열은
    script.value — 조사자가 읽어 준 **질문**이다. 그대로 두면 어르신이 한 말
    대신 질문지를 정답으로 삼아 채점한다(실측으로 확인했다).

    sentences 를 먼저 쓴다(문장 단위라 이어 붙일 때 자연스럽다). 없으면
    segments 로 내려간다. **dialect 를 쓴다** — 표준형이 아니라 실제로 말한
    쪽을 재는 것이 이 스크립트의 목적이다(위 KEYS 주석과 같은 이유).
    """
    if not isinstance(data, dict):
        return None
    tr = data.get("transcription")
    if not isinstance(tr, dict):
        return None
    for key in ("sentences", "segments"):
        rows = tr.get(key)
        if not isinstance(rows, list):
            continue
        parts = [r["dialect"].strip() for r in rows
                 if isinstance(r, dict) and isinstance(r.get("dialect"), str)
                 and r["dialect"].strip()]
        if parts:
            return " ".join(parts)
    return None


def _load_transcript(path: str) -> str | None:
    """라벨 파일에서 전사문을 꺼낸다.

    공개 데이터셋마다 키 이름이 다르다. 스키마를 미리 알 수 없으므로 흔한
    키를 순서대로 훑고, 없으면 가장 긴 문자열 값을 쓴다 — 전사문이 보통
    그 파일에서 제일 긴 문자열이다. 못 찾으면 조용히 건너뛴다.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    if not path.endswith(".json"):
        return raw.strip() or None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # 조각으로 들어 있는 스키마를 먼저 본다 — 폴백에 맡기면 틀린 것을 집는다.
    stitched = _stitched_transcript(data)
    if stitched:
        return stitched

    KEYS = ("transcription", "standard_form", "text", "sentence",
            "orgtext", "dialect_form", "form", "script")

    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and k.lower() in KEYS and v.strip():
                    found.append(v.strip())
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and node.strip():
            found.append(node.strip())

    # 우선순위 키를 먼저 본다 — 방언 데이터는 '방언형'과 '표준형'이 같이
    # 들어 있는 경우가 많은데, 우리가 재려는 것은 실제로 말한 쪽이다.
    if isinstance(data, dict):
        for key in KEYS:
            hit = _first_key(data, key)
            if hit:
                return hit
    walk(data)
    return max(found, key=len) if found else None


def _first_key(node: object, key: str) -> str | None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() == key and isinstance(v, str) and v.strip():
                return v.strip()
            got = _first_key(v, key)
            if got:
                return got
    elif isinstance(node, list):
        for v in node:
            got = _first_key(v, key)
            if got:
                return got
    return None


def find_pairs(root: str, limit: int) -> list[tuple[str, str]]:
    """오디오와 같은 이름의 라벨 파일을 짝지어 모은다."""
    pairs: list[tuple[str, str]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if not name.lower().endswith(AUDIO_EXT):
                continue
            audio = os.path.join(dirpath, name)
            stem = os.path.splitext(name)[0]
            for cand in (f"{stem}.json", f"{stem}.txt"):
                label = os.path.join(dirpath, cand)
                if os.path.exists(label):
                    pairs.append((audio, label))
                    break
            if limit and len(pairs) >= limit:
                return pairs
    return pairs


# --------------------------------------------------------- 전화 조건 재현 --

def have_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


def to_telephony(src: str, dst: str) -> bool:
    """8kHz μ-law 로 내렸다가 16kHz 로 되올린다.

    중간 결과를 파이프가 아니라 임시 파일로 넘긴다. WAV 헤더는 크기를
    되돌아가 적어야 하는데 파이프는 seek 이 안 돼서, 환경에 따라 두 번째
    ffmpeg 가 길이를 0 으로 읽는다. 파일이면 그 문제가 없다.
    """
    mid = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    mid.close()
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
             # 8kHz 모노 μ-law — 전화망 그대로. 여기서 4kHz 위가 사라진다.
             "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", mid.name],
            capture_output=True, check=True)
        # 되올린다. 잘려나간 대역은 돌아오지 않는다 — Whisper 입력이 16kHz
        # 여야 해서 맞춰줄 뿐이다.
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", mid.name,
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst],
            capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    finally:
        os.unlink(mid.name)
    return True


# ------------------------------------------------------------------ 지표 --

_PUNCT = re.compile(r"[.,!?~…·\"'“”‘’()\[\]{}]")


def normalize(s: str, keep_space: bool = False) -> str:
    """비교 전 정규화. 구두점은 STT 가 임의로 붙이므로 양쪽에서 뺀다."""
    out = _PUNCT.sub("", s).strip()
    out = re.sub(r"\s+", " ", out)
    return out if keep_space else out.replace(" ", "")


def edit_distance(a: list[str] | str, b: list[str] | str) -> int:
    """레벤슈타인. 문자열이면 글자 단위, 리스트면 토큰 단위."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ------------------------------------------------------------------ 본체 --

def main() -> int:
    ap = argparse.ArgumentParser(description="STT 성능 측정")
    ap.add_argument("--data", required=True, help="오디오+라벨이 있는 디렉토리")
    ap.add_argument("--limit", type=int, default=100, help="측정할 파일 수 (0=전부)")
    ap.add_argument("--no-telephony", action="store_true",
                    help="전화 조건 재현 없이 원본 그대로 (비교용)")
    ap.add_argument("--errors", help="틀린 건을 TSV 로 저장할 경로")
    ap.add_argument("--max-seconds", type=float, default=30.0,
                    help="이보다 긴 녹음은 건너뛴다 (자유대화는 통짜로 길다)")
    args = ap.parse_args()

    from donghaenggori.services import stt

    if not stt.available():
        print("faster-whisper 가 없습니다. GPU 서버에서 돌리세요.", file=sys.stderr)
        return 2

    # 전사를 다 돌린 뒤에 ffmpeg 이 없다는 걸 알면 몇 시간을 버린다.
    if not args.no_telephony and not have_ffmpeg():
        print("ffmpeg 가 없습니다 — 전화 조건을 재현할 수 없습니다.", file=sys.stderr)
        print("  apt install ffmpeg   (또는 --no-telephony 로 원본 측정)",
              file=sys.stderr)
        return 2

    pairs = find_pairs(args.data, args.limit)
    if not pairs:
        print(f"오디오+라벨 짝을 찾지 못했습니다: {args.data}", file=sys.stderr)
        print("오디오와 같은 이름의 .json/.txt 가 같은 폴더에 있어야 합니다.",
              file=sys.stderr)
        return 1

    mode = "원본(광대역)" if args.no_telephony else "8kHz μ-law 전화 재현"
    print(f"모델 {stt.MODEL_SIZE} · {stt.DEVICE} · {mode}")
    print(f"대상 {len(pairs)}건\n")

    tot_c = tot_cn = tot_w = tot_wn = 0
    rows: list[tuple[float, str, str, str]] = []
    wrong_tokens: Counter[tuple[str, str]] = Counter()
    skipped = 0

    for i, (audio, label) in enumerate(pairs, 1):
        truth = _load_transcript(label)
        if not truth:
            skipped += 1
            continue

        dur = stt.probe_duration(audio)
        if dur is not None and dur > args.max_seconds:
            skipped += 1
            continue

        path, tmp = audio, None
        if not args.no_telephony:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            if not to_telephony(audio, tmp.name):
                print("ffmpeg 실행 실패 — 설치되어 있는지 확인하세요.", file=sys.stderr)
                os.unlink(tmp.name)
                return 2
            path = tmp.name

        try:
            got = stt.transcribe(path).text
        except Exception as exc:                      # noqa: BLE001
            print(f"  [{i}] 전사 실패 {os.path.basename(audio)}: {exc}")
            skipped += 1
            continue
        finally:
            if tmp:
                os.unlink(tmp.name)

        # 글자 단위 — 공백 뺀 쪽이 우리에게 의미 있는 지표다.
        t, g = normalize(truth), normalize(got)
        tot_c += edit_distance(t, g)
        tot_cn += len(t)

        # 어절 단위 — 참고용.
        tw, gw = normalize(truth, True).split(), normalize(got, True).split()
        tot_w += edit_distance(tw, gw)
        tot_wn += len(tw)

        cer = edit_distance(t, g) / len(t) if t else 0.0
        if cer > 0:
            rows.append((cer, os.path.basename(audio), truth, got))
            # 길이가 같은 어절끼리만 맞춰 본다. 정렬 없이 세는 거라 정확하진
            # 않지만, 자주 틀리는 단어를 찾는 데는 충분하다.
            if len(tw) == len(gw):
                for a, b in zip(tw, gw, strict=True):
                    if a != b:
                        wrong_tokens[(a, b)] += 1

        if i % 20 == 0:
            print(f"  {i}/{len(pairs)} …")

    if tot_cn == 0:
        print("측정할 수 있는 건이 없었습니다.", file=sys.stderr)
        return 1

    n = len(pairs) - skipped
    print(f"\n{'=' * 52}")
    print(f"CER (공백 제외)  {tot_c / tot_cn:.3f}   ← 주 지표")
    print(f"WER (어절)       {tot_w / tot_wn:.3f}   ← 참고 (띄어쓰기에 민감)")
    print(f"측정 {n}건 · 건너뜀 {skipped}건 · 완전 일치 {n - len(rows)}건")
    print("=" * 52)

    if wrong_tokens:
        print("\n자주 틀리는 단어 (정답 → 인식) — hotwords·_FIXUPS 후보")
        for (a, b), c in wrong_tokens.most_common(25):
            print(f"  {c:3d}회  {a}  →  {b}")

    if args.errors:
        rows.sort(reverse=True)
        with open(args.errors, "w", encoding="utf-8") as f:
            f.write("cer\tfile\ttruth\tstt\n")
            for cer, name, truth, got in rows:
                f.write(f"{cer:.3f}\t{name}\t{truth}\t{got}\n")
        print(f"\n틀린 {len(rows)}건 → {args.errors}")
        print("개인정보가 들어 있을 수 있습니다. 커밋하지 마세요.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
