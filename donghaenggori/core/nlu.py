"""발화 이해 (하이브리드 NLU) — 계획서 6장.

구조: 규칙 기반 파서(항상 동작) + Claude LLM(키 있으면 발화 의도·진료과·증상·긴급신호 보강).
- 의도 분류: 병원동행 / 약국 / 보호자연락 / 긴급 / 기타
- 슬롯 추출: 진료과, 증상 키워드, (날짜는 dateparse가 결정적으로 처리)
- 긴급 신호: 감지 시 즉시 '사람 연결'로 전환 (AI는 응급 여부를 판단하지 않음)

API 키(ANTHROPIC_API_KEY)가 없으면 규칙 기반만으로도 동작한다(데모 가능).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import dateparse

_TERMS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "medical_terms.json")
with open(_TERMS, encoding="utf-8") as _f:
    TERMS = json.load(_f)

INTENTS = ["병원동행", "약국", "보호자연락", "긴급", "기타"]


@dataclass
class Analysis:
    intent: str = "병원동행"
    dept: str | None = None          # 진료과
    symptom: str | None = None       # 증상 키워드
    date: dict | None = None         # {"date","label","confident"}
    urgent: bool = False             # 긴급 신호
    source: str = "규칙"             # "규칙" 또는 "규칙+LLM"
    raw: str = ""
    notes: list[str] = field(default_factory=list)
    # 대리 접수 — 보호자·기관이 어르신 대신 전화한 경우
    requester: str = "본인"          # 본인 | 대리
    proxy_relation: str | None = None  # 어머니 | 아버지 | 조부모 | 배우자 | 기타


def detect_proxy(text: str) -> tuple[str, str | None]:
    """대리 요청 여부와 추정 관계를 판별한다.

    "느그 어매 병원 좀 델꼬 가야 쓰겄는디" → ("대리", "어머니")
    관계 호칭만 있어도 대리로 본다. 대상자 확정은 사회복지사가 한다.
    """
    pk = TERMS.get("proxy_keywords") or {}
    for relation, words in (pk.get("relation") or {}).items():
        if relation.startswith("_"):
            continue
        if any(w in text for w in words):
            return "대리", relation
    if any(v in text for v in (pk.get("proxy_verbs") or [])):
        return "대리", None
    return "본인", None


# ---------------------------------------------------------------- 규칙 기반 ----

def _rule_based(text: str) -> Analysis:
    a = Analysis(raw=text)

    # 긴급 신호 — 가장 먼저, 보수적으로(조금이라도 의심되면 긴급)
    for kw in TERMS["urgent_keywords"]:
        if kw in text:
            a.urgent = True
            a.intent = "긴급"
            return a

    # 진료과: 직접 언급 우선
    for dept in TERMS["dept_keywords"]:
        if dept in text:
            a.dept = dept
            break
    # 증상 → 진료과 (직접 언급이 없을 때)
    for sym, dept in TERMS["symptom_to_dept"].items():
        if sym in text:
            a.symptom = sym
            if a.dept is None:
                a.dept = dept
            break

    # 의도: 약국 / 보호자연락 키워드
    for intent, kws in TERMS["intent_keywords"].items():
        if any(kw in text for kw in kws):
            a.intent = intent
            break

    # 대리 접수 판별 (긴급 다음으로 중요 — 대상자 확정에 영향)
    a.requester, a.proxy_relation = detect_proxy(text)

    # 날짜(결정적)
    a.date = dateparse.parse_date(text)
    return a


# ----------------------------------------------------------------- LLM 보강 ----

def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _llm_refine(text: str, base: Analysis) -> Analysis:
    """Claude로 의도·진료과·증상·긴급신호를 보강. 날짜는 규칙 결과를 유지한다."""
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        base.notes.append("anthropic/pydantic 미설치 — 규칙 기반만 사용")
        return base

    class Parsed(BaseModel):
        intent: str         # 병원동행 | 약국 | 보호자연락 | 긴급 | 기타
        dept: str | None    # 진료과(예: 정형외과). 모르면 null
        symptom: str | None # 증상 키워드(예: 무릎). 없으면 null
        urgent: bool        # 가슴통증/호흡곤란/쓰러짐 등 긴급 신호 여부

    system = (
        "너는 노인 병원동행 콜센터의 발화 분석기다. 어르신의 짧고 모호한 전화 발화에서 "
        "의도와 핵심 정보를 추출한다. 의료 진단을 하지 말고, 발화에 드러난 사실만 추출한다. "
        "긴급 신호(가슴통증·호흡곤란·쓰러짐·의식저하 등)는 조금이라도 의심되면 urgent=true로 둔다. "
        f"intent는 반드시 다음 중 하나: {INTENTS}."
    )
    try:
        client = anthropic.Anthropic()
        resp = client.messages.parse(
            model="claude-opus-4-8",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": f'발화: "{text}"'}],
            output_format=Parsed,
        )
        p = resp.parsed_output
        if p is None:
            base.notes.append("LLM 파싱 실패 — 규칙 결과 사용")
            return base
        merged = Analysis(
            intent=p.intent if p.intent in INTENTS else base.intent,
            dept=p.dept or base.dept,
            symptom=p.symptom or base.symptom,
            date=base.date,                 # 날짜는 규칙 파서 결과 유지
            urgent=bool(p.urgent) or base.urgent,
            source="규칙+LLM",
            raw=text,
            notes=base.notes,
        )
        if merged.urgent:
            merged.intent = "긴급"
        return merged
    except Exception as e:  # 네트워크/키 오류 등 — 규칙 결과로 폴백
        base.notes.append(f"LLM 오류로 규칙 결과 사용: {type(e).__name__}")
        return base


# ------------------------------------------------------------------- 공개 API --

def analyze(text: str, use_llm: bool | None = None) -> Analysis:
    base = _rule_based(text)
    if base.urgent:                 # 긴급이면 LLM 호출 없이 즉시 반환(빠른 사람 연결)
        return base
    if use_llm is None:
        use_llm = _llm_available()
    if use_llm:
        return _llm_refine(text, base)
    return base
