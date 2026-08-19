"""STT 측정 도구의 계산이 맞는지 — 숫자를 믿기 전에.

    python -m tests.test_stt_eval

이 도구가 내는 CER 로 파인튜닝을 할지 말지를 정한다. 계산이 틀리면 그
판단이 통째로 틀린다. 모델 없이 확인할 수 있는 부분(정규화·편집거리·
라벨 파싱)만 여기서 고정한다 — 전사 자체는 GPU 서버에서만 돈다.
"""
from __future__ import annotations

import json
import os
import tempfile

from tools.stt_eval import _load_transcript, edit_distance, find_pairs, normalize


def test_normalize() -> None:
    # 구두점은 STT 가 임의로 붙인다. 양쪽에서 빼지 않으면 없던 오류가 생긴다.
    assert normalize("안녕 하세요.") == "안녕하세요"
    assert normalize("어, 병원 좀…") == "어병원좀"
    assert normalize("안녕  하세요!", keep_space=True) == "안녕 하세요"
    print("  정규화 4건 ok")


def test_edit_distance() -> None:
    assert edit_distance("배", "비") == 1
    assert edit_distance("정형외과", "정형외과") == 0
    assert edit_distance("", "abc") == 3
    assert edit_distance("abc", "") == 3
    # 토큰(어절) 단위도 같은 함수로 잰다
    assert edit_distance(["가", "나"], ["가", "다"]) == 1
    assert edit_distance(["가"], ["가", "나"]) == 1
    print("  편집거리 6건 ok")


def test_cer_matches_hand_count() -> None:
    """실제로 보고된 오인식으로 CER 을 손으로 세어 맞춘다."""
    truth, got = "낼 배가 아파서 병원 가야겄어", "낼 비가 아파서 병원 가야겄어"
    t, g = normalize(truth), normalize(got)
    assert len(t) == 12, len(t)
    assert edit_distance(t, g) == 1            # 배 → 비, 한 글자
    assert abs(edit_distance(t, g) / len(t) - 0.0833) < 0.001

    # 띄어쓰기만 다른 경우 — CER 은 0 이어야 한다. WER 이면 오답으로 세는데,
    # 슬롯 추출에는 영향이 없으므로 CER 을 주 지표로 쓰는 이유가 여기 있다.
    assert edit_distance(normalize("정형 외과 갑시다"), normalize("정형외과 갑시다")) == 0
    print("  CER 손계산 일치 ok")


def test_label_schemas() -> None:
    """스키마를 모르는 채로도 전사문을 찾아낸다."""
    d = tempfile.mkdtemp()
    cases = [
        ({"transcription": "낼 병원 가야겄어"}, "낼 병원 가야겄어"),
        ({"utterance": [{"dialect_form": "그 뭐시기 눈 보는 데"}]},
         "그 뭐시기 눈 보는 데"),
        ({"meta": {"id": "x"}, "script": {"text": "모레 정형외과 갑시다"}},
         "모레 정형외과 갑시다"),
        # 아는 키가 하나도 없으면 가장 긴 문자열로 떨어진다
        ({"id": "a1", "말": "어르신 병원 같이 가주쇼"}, "어르신 병원 같이 가주쇼"),
    ]
    for i, (obj, want) in enumerate(cases):
        p = os.path.join(d, f"{i}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        assert _load_transcript(p) == want, _load_transcript(p)

    # 깨진 파일은 죽지 않고 건너뛴다 — 수만 건 중 몇 개는 반드시 깨져 있다.
    bad = os.path.join(d, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ 이건 json 이 아니다")
    assert _load_transcript(bad) is None
    print("  라벨 파싱 5건 ok")


def test_find_pairs() -> None:
    d = tempfile.mkdtemp()
    sub = os.path.join(d, "안쪽")
    os.makedirs(sub)
    for name in ("a.wav", "b.flac"):
        with open(os.path.join(sub, name), "wb") as f:
            f.write(b"RIFF")
    with open(os.path.join(sub, "a.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(sub, "b.txt"), "w") as f:
        f.write("x")
    # 짝이 없는 오디오는 세지 않는다
    with open(os.path.join(sub, "c.wav"), "wb") as f:
        f.write(b"RIFF")

    assert len(find_pairs(d, 0)) == 2
    assert len(find_pairs(d, 1)) == 1          # --limit 이 실제로 잘린다
    print("  짝 찾기 2건 ok")


def main() -> None:
    print("STT 측정 도구")
    test_normalize()
    test_edit_distance()
    test_cer_matches_hand_count()
    test_label_schemas()
    test_find_pairs()
    print("전부 통과")


if __name__ == "__main__":
    main()
