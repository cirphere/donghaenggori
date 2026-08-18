"""C-DS01(7.8GB) → 학습·평가에 필요한 텍스트만 담은 작은 파일.

    python -m tools.export_cds01 --data <C-DS01 경로> --out cds01.jsonl.gz

원본은 JSON 파일 200만 개다. 학습은 GPU가 있는 기기에서 하고 싶은데 데이터셋을
통째로 옮기는 건 현실적이지 않아서, **쓰는 것만 뽑아 둔다**(수십 MB).

담는 것 — AI-Hub 가 나눠 둔 TL(학습)/VL(검증) 구분을 그대로 유지한다.
  · kind="session"   세션 도입부 N발화 + 의도.  학습과 세션 기준 평가에 쓴다.
  · kind="utterance" 고객 발화 하나 + 의도.     제출 문서가 적어 둔 '검증셋
                     전량' 기준이 이쪽이라, VL 만 담는다.

원본 음성이나 화자 정보는 담지 않는다. 상담 텍스트가 그대로 들어가므로
저장소에 커밋하지 않는다(.gitignore).
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib


def run(data: str, out: str, opening: int = 5) -> dict:
    from donghaenggori.services import cds01

    counts: dict[str, int] = {}
    path = pathlib.Path(out)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for split in ("TL", "VL"):
            X, y, _ = cds01.load_sessions(data, opening=opening, split=split)
            for text, label in zip(X, y, strict=True):
                f.write(json.dumps({"kind": "session", "split": split,
                                    "text": text, "label": label},
                                   ensure_ascii=False) + "\n")
            counts[f"session/{split}"] = len(X)

        # 발화 단위는 검증셋만. 학습에는 쓰지 않으므로(라벨 노이즈) 담을 이유가 없고,
        # TL 까지 담으면 파일이 수백 MB 로 불어난다.
        Xu, yu, repu = cds01.load(data, speaker="고객", split="VL")
        for text, label in zip(Xu, yu, strict=True):
            f.write(json.dumps({"kind": "utterance", "split": "VL",
                                "text": text, "label": label},
                               ensure_ascii=False) + "\n")
        # 제출 문서는 "검증셋 129,029건 전량" 이라고 적었다. 그 숫자는 VL 의
        # **고객 발화 전체**다. 우리 과제는 4개 의도 분류라, 그 밖의 상담
        # 카테고리는 라벨이 없어 평가에 쓸 수 없다. 몇 건이 왜 빠졌는지
        # 남겨 두어야 "전량으로 쟀다" 고 잘못 말하지 않는다.
        counts["utterance/VL"] = len(Xu)
        counts["VL고객발화(문서기준)"] = repu.total_files - repu.skipped_speaker
        counts["└ 너무 짧아 제외"] = repu.skipped_short
        counts["└ 의도 매핑 없음"] = repu.skipped_unmapped

    counts["파일크기MB"] = round(path.stat().st_size / 1048576, 1)
    return counts


def load(path: str, kind: str, split: str) -> tuple[list[str], list[str]]:
    """내보낸 파일 읽기 — 실제 구현은 services.cds01 에 있다.

    컨테이너 이미지에 tools/ 가 들어가지 않아서, 학습이 읽어야 하는 코드는
    서비스 쪽에 두어야 한다. 여기서는 예전 호출부를 위해 넘겨주기만 한다.
    """
    from donghaenggori.services import cds01
    return cds01.load_export(path, kind, split)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="C-DS01 라벨 디렉터리")
    ap.add_argument("--out", default="cds01.jsonl.gz")
    ap.add_argument("--opening", type=int, default=5)
    a = ap.parse_args()
    for k, v in run(a.data, a.out, a.opening).items():
        print(f"  {k:18} {v:,}" if isinstance(v, int) else f"  {k:18} {v}")
    print(f"\n  저장: {a.out}")


if __name__ == "__main__":
    main()
