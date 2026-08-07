"""AI-Hub C-DS01 복지 분야 콜센터 상담데이터 파서.

원본 구조
    <root>/<도메인>/<category1>/<category2>/<category3>/<세션ID>/<발화ID>.json

JSON 스키마
    inputText[0].orgtext              발화 텍스트
    info[0].metadata.category1/2/3    3계층 분류
    info[0].metadata.speaker_type     고객 | 상담사
    info[0].metadata.speaker_age/sex  화자 속성

우리 서비스는 **어르신(고객)의 요청 발화**를 분류한다. 상담사 발화는 응대 스크립트라
성격이 다르므로 기본적으로 제외한다.

의도 매핑 근거
  · 차량요청           → 병원동행  (이동지원센터에 "병원 가야 한다"고 거는 전화가 여기 모임)
  · 진료안내(검사/입원/외래/건강검진) → 병원동행
  · 응급, 자살위기개입   → 긴급     (사람에게 즉시 넘겨야 하는 발화)
  · 그 외(민원·규정문의·정신건강상담) → 기타  (학습에서 제외하거나 음성 클래스로 사용)

'약국'·'보호자연락'은 원본에 대응 카테고리가 없다. 규칙 사전 기반 합성 데이터로
보완하며, 이 사실을 학습 리포트에 명시한다(없는 라벨을 지어내지 않는다).
"""
from __future__ import annotations

import collections
import json
import pathlib
from dataclasses import dataclass, field

# category3 → 우리 의도
CATEGORY_TO_INTENT: dict[str, str] = {
    # 이동 요청 — 병원동행 접수의 실제 표본
    "차량요청": "병원동행",
    "예약변경 및 취소": "병원동행",
    # 병원 진료 관련
    "검사": "병원동행",
    "입원": "병원동행",
    "외래": "병원동행",
    "건강검진": "병원동행",
    "입퇴원": "병원동행",
    # 즉시 사람에게 넘겨야 하는 발화
    "응급": "긴급",
    "가정불화": "긴급",
    "경제문제": "긴급",
    "이성문제": "긴급",
    "신체정신적문제": "긴급",
    "직장문제": "긴급",
    "외로움고독": "긴급",
    "학교성적진로": "긴급",
    "친구동료문제": "긴급",
}

# 자살위기개입은 category2 단위로도 긴급 처리 (category3가 '기타'인 경우 대응)
CATEGORY2_TO_INTENT: dict[str, str] = {"자살위기개입": "긴급"}

MIN_CHARS = 5          # 너무 짧은 발화("네", "예")는 학습에 방해


@dataclass
class LoadReport:
    total_files: int = 0
    parsed: int = 0
    skipped_speaker: int = 0
    skipped_short: int = 0
    skipped_unmapped: int = 0
    errors: int = 0
    intent_counts: collections.Counter = field(default_factory=collections.Counter)
    category_counts: collections.Counter = field(default_factory=collections.Counter)

    def summary(self) -> str:
        L = [
            f"파일 {self.total_files:,}개 스캔",
            f"  사용     {self.parsed:,}건",
            f"  제외     화자(상담사) {self.skipped_speaker:,} / "
            f"짧은발화 {self.skipped_short:,} / 미매핑 {self.skipped_unmapped:,} / 오류 {self.errors:,}",
            "",
            "의도 분포:",
        ]
        tot = sum(self.intent_counts.values()) or 1
        for k, v in self.intent_counts.most_common():
            L.append(f"  {k:<8} {v:>8,} ({v/tot*100:5.1f}%)")
        return "\n".join(L)


def load_sessions(root: str | pathlib.Path, opening: int | None = None,
                  speaker: str = "고객") -> tuple[list[str], list[str], LoadReport]:
    """세션(통화) 단위로 묶어 (텍스트, 의도) 쌍을 만든다.

    라벨이 통화 단위로 붙어 있으므로 발화 단위 학습은 노이즈가 크다.
    (차량요청 통화 안의 잡담까지 전부 '병원동행'이 되어버린다)

    opening: 앞쪽 N개 고객 발화만 사용. 우리 서비스는 어르신의 짧은 첫 요청을
             분류하므로, 통화 전체보다 도입부가 실사용에 가깝다.
             None이면 통화 전체를 이어붙인다.
    """
    root = pathlib.Path(root)
    rep = LoadReport()
    sessions: dict[pathlib.Path, dict] = {}

    for p in root.rglob("*.json"):
        rep.total_files += 1
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            text = (d["inputText"][0].get("orgtext") or "").strip()
            m = d["info"][0]["metadata"]
        except Exception:
            rep.errors += 1
            continue
        if m.get("speaker_type") != speaker:
            rep.skipped_speaker += 1
            continue
        if len(text) < MIN_CHARS:
            rep.skipped_short += 1
            continue
        s = sessions.setdefault(p.parent, {"utts": [], "c2": m.get("category2", ""),
                                           "c3": m.get("category3", "")})
        s["utts"].append((p.name, text))

    X: list[str] = []
    y: list[str] = []
    for meta in sessions.values():
        intent = CATEGORY_TO_INTENT.get(meta["c3"]) or CATEGORY2_TO_INTENT.get(meta["c2"])
        rep.category_counts[f"{meta['c2']}/{meta['c3']}"] += 1
        if intent is None:
            rep.skipped_unmapped += 1
            continue
        utts = [t for _, t in sorted(meta["utts"])]
        if opening:
            utts = utts[:opening]
        X.append(" ".join(utts))
        y.append(intent)
        rep.intent_counts[intent] += 1
        rep.parsed += 1
    return X, y, rep


def load(root: str | pathlib.Path, speaker: str | None = "고객",
         limit: int | None = None) -> tuple[list[str], list[str], LoadReport]:
    """발화 단위 로더 — 라벨 노이즈가 크므로 참고용. 학습에는 load_sessions를 쓴다.

    speaker: '고객'만 쓰려면 그대로. None이면 화자 구분 없이 전부.
    """
    root = pathlib.Path(root)
    rep = LoadReport()
    X: list[str] = []
    y: list[str] = []

    for p in root.rglob("*.json"):
        rep.total_files += 1
        if limit and rep.parsed >= limit:
            break
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            text = (d["inputText"][0].get("orgtext") or "").strip()
            m = d["info"][0]["metadata"]
        except Exception:
            rep.errors += 1
            continue

        if speaker and m.get("speaker_type") != speaker:
            rep.skipped_speaker += 1
            continue
        if len(text) < MIN_CHARS:
            rep.skipped_short += 1
            continue

        c2, c3 = m.get("category2", ""), m.get("category3", "")
        rep.category_counts[f"{c2}/{c3}"] += 1
        intent = CATEGORY_TO_INTENT.get(c3) or CATEGORY2_TO_INTENT.get(c2)
        if intent is None:
            rep.skipped_unmapped += 1
            continue

        X.append(text)
        y.append(intent)
        rep.intent_counts[intent] += 1
        rep.parsed += 1

    return X, y, rep
