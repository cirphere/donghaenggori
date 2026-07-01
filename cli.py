#!/usr/bin/env python3
"""동행고리 AI — 터미널 데모.

사용법:
  python cli.py                          # 기본 데모 시나리오 4개 실행
  python cli.py "010-1234-5678" "모레 정형외과 가야겄어"   # 직접 입력
"""
import sys

from donghaenggori import pipeline

DEMOS = [
    ("010-1234-5678", "모레 정형외과 가야겄어"),          # 단골 → 확인됨
    ("010-1234-5678", "저번에 무릎 봐준 데 또 가야 쓰겄어"),  # 이력 해석 → 확인됨/추정
    ("010-7777-8888", "다음주 화요일에 병원 가야 하는디"),    # 신규+진료과 모름 → 확인 필요
    ("010-1234-5678", "가슴이 아파서 숨이 차"),            # 긴급 → 사람 연결
]


def show(phone, utterance):
    print("=" * 60)
    print(f"📞 발신: {phone}   🗣  발화: \"{utterance}\"")
    r = pipeline.run(phone, utterance)
    print(f"   (발화 분석: {r.analysis.source})")
    if r.urgent:
        print(r.urgent_message)
    else:
        print(r.card.to_text())
    print()


def main():
    if len(sys.argv) >= 3:
        show(sys.argv[1], sys.argv[2])
    else:
        for phone, utt in DEMOS:
            show(phone, utt)


if __name__ == "__main__":
    main()
