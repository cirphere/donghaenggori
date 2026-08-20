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
import re
from dataclasses import dataclass, field, replace

from ..config import settings
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
    hospital: str | None = None      # 발화에 직접 나온 병원명 (이력보다 우선)
    date: dict | None = None         # {"date","label","confident","corrected"}
    time: dict | None = None         # {"time","label","confident","corrected"}
    urgent: bool = False             # 긴급 신호
    source: str = "규칙"             # "규칙" 또는 "규칙+LLM"
    raw: str = ""
    notes: list[str] = field(default_factory=list)
    # 진료과를 어디서 얻었나 — 'spoken'(직접 말함) | 'dict'(증상 사전)
    # | 'embedding'(의미 추정) | None. 카드가 상태를 가를 때 쓴다.
    dept_source: str | None = None
    # 대리 접수 — 보호자·기관이 어르신 대신 전화한 경우
    requester: str = "본인"          # 본인 | 대리
    proxy_relation: str | None = None  # 어머니 | 아버지 | 조부모 | 배우자 | 기타


# 관계 호칭 뒤에 올 수 있는 말. 여기 없는 글자가 이어지면 **다른 낱말**이다 —
# "어머니날", "엄마손", "할머니회" 를 대리 요청으로 보면 안 된다.
_AFTER_RELATION = ("가", "이", "는", "은", "를", "을", "도", "만", "의", "와", "과",
                   "랑", "한테", "에게", "께", "하고", "님", "요", ",", ".", "!", "?")


def _mentions_relation(text: str, word: str) -> bool:
    """관계 호칭이 **낱말로** 나왔는가.

    부분문자열로만 보면 "어머니날에 병원 가야" 가 어머니 대리 요청이 된다.
    호칭 뒤가 조사·공백·문장 끝이어야 진짜 호칭으로 본다.
    """
    start = 0
    while (i := text.find(word, start)) != -1:
        end = i + len(word)
        if end >= len(text) or text[end].isspace() or text.startswith(_AFTER_RELATION, end):
            return True
        start = i + 1
    return False


def detect_proxy(text: str) -> tuple[str, str | None]:
    """대리 요청 여부와 추정 관계를 판별한다.

    "우리 어매 병원 좀 델꼬 가야 쓰겄는디" → ("대리", "어머니")
    관계 호칭만 있어도 대리로 본다. 대상자 확정은 사회복지사가 한다.

    **놓치는 쪽이 더 나쁘다.** 대리를 못 잡으면 발신자의 병원 이력과 등급이
    남의 접수에 붙는다(pipeline 이 대리일 때 프로필을 버린다). 반대로 잘못
    잡으면 카드가 '확인 필요' 로 남을 뿐이라, 애매하면 대리로 본다.
    다만 낱말 경계는 지킨다 — 그건 애매한 게 아니라 그냥 오탐이다.
    """
    pk = TERMS.get("proxy_keywords") or {}
    for relation, words in (pk.get("relation") or {}).items():
        if relation.startswith("_"):
            continue
        if any(_mentions_relation(text, w) for w in words):
            return "대리", relation
    if any(v in text for v in (pk.get("proxy_verbs") or [])):
        return "대리", None
    return "본인", None


# 병원 이름의 꼬리. 긴 것부터 본다 — "대학교병원" 을 "병원" 으로 자르면 안 된다.
_HOSPITAL_SUFFIX = ("대학교병원", "대학병원", "종합병원", "한방병원", "치과병원",
                    "요양병원", "노인병원", "의료원", "보건지소", "보건소",
                    "한의원", "병원", "의원", "클리닉")

# 꼬리 앞에 붙어도 이름이 아닌 말들. "저번에 갔던 병원" 의 '갔던' 을 이름으로
# 삼으면 안 된다. 어르신은 병원 이름을 잘 안 대고 이런 표현을 자주 쓴다.
_NOT_A_NAME = {"그", "저", "저번", "지난번", "예전", "전", "전에", "갔던", "간", "봐준",
               "보던", "다니던", "다닌", "큰", "작은", "동네", "근처", "가까운",
               "같은", "어느", "무슨", "새", "다른", "이", "저기", "거기",
               # 시간 표현. "내일 병원 가야 해요" 가 '내일병원' 이라는 없는
               # 병원을 만들어냈고, 발화에 직접 나왔다는 이유로 '확인됨' 까지
               # 붙었다. 어르신이 병원 이름 없이 날짜만 말하는 것이 오히려
               # 흔하다 — 그때는 병원을 비워 두고 이력에서 찾아야 한다.
               "오늘", "낼", "내일", "모레", "모래", "글피", "어제", "그제",
               "아까", "이따", "지금", "이번주", "다음주", "담주", "저번주",
               "이번달", "다음달", "요번", "이번", "매주", "곧",
               # 가족 호칭. "우리 어매 병원 좀 델꼬 가야 쓰겄는디" 가 '어매병원'
               # 이라는 없는 병원을 만들어 '확인됨' 으로 띄웠다. 대리 접수에서
               # 가장 흔한 말투인데, 정작 그 말을 병원 이름으로 먹었다.
               "아들", "딸", "며느리", "사위", "손자", "손녀", "형", "누나",
               "언니", "오빠", "동생", "부모", "자식", "가족", "본인", "제가"}

# 대리 판별이 이미 알고 있는 관계 호칭을 그대로 가져온다("어매" → 어머니).
# 같은 목록을 두 군데 적으면 한쪽만 늘어난다 — 실제로 병원명 쪽만 몰라서
# '어매병원' 이 나왔다.
_NOT_A_NAME |= {
    w for words in ((TERMS.get("proxy_keywords") or {}).get("relation") or {}).values()
    for w in words if " " not in w
}

# 이름 + 꼬리. 이름은 **한 덩어리**만 잡는다 — 두 덩어리를 허용하면
# "내일 송정병원" 의 '내일' 까지 먹는다. 꼬리 앞 띄어쓰기는 잡되, 띄어 쓴
# 경우를 그대로 인정하지는 않는다(아래 _is_real_name 참조).
_HOSPITAL_RE = re.compile(
    r"([가-힣A-Za-z0-9]+)(\s*)(" + "|".join(_HOSPITAL_SUFFIX) + r")")

# 어르신은 말하면서 고친다 — "송정병원 말고 목포한국병원". 날짜 파서와 같은 기준.
_CORRECTION = re.compile(r"아니|말고|말구|아니라|아니고")

# 이름 **뒤에** 이 말이 오면 그 병원을 말한 것이 아니라 **아니라고 말한 것**이다.
#
#   "백병원에는 피부과가 없으니 다른 병원을 추천해 주세요"
#
# 실통화에서 이 문장의 '백병원' 을 직접 언급으로 먹어 '확인됨' 까지 올라갔고,
# 통화 마지막에 "백병원으로 접수했습니다" 를 들려줬다. 어르신은 그 병원에
# 피부과가 없다고 말한 것이다.
_NEGATED_AFTER = ("없", "말고", "말구", "아니", "안 가", "못 가", "빼고", "아닌", "싫")

# 부정어를 찾을 범위. 이름 바로 뒤 한 마디 정도만 본다 — 문장 끝까지 보면
# "송정병원으로 가야 하는데 시간이 없어요" 까지 부정으로 걸린다.
_NEGATION_WINDOW = 14

# 띄어 쓰지 않은 '병원/의원/클리닉' 은 상호로 본다("송정병원"). 띄어 쓴 것은
# 대개 상호가 아니라 설명이다("좋아서 병원", "목포 병원", "어매 병원").
_GENERIC_SUFFIX = {"병원", "의원", "클리닉"}

# 띄어 썼어도 상호로 인정하는 앞말. "전남대학교 병원" 은 진짜 상호다.
_NAME_ENDINGS = ("대학교", "대학", "의료원", "재단")

# 행정구역으로 끝나면 지명이다 — "하의면 보건지소", "고흥군 보건소".
_PLACE_ENDINGS = ("읍", "면", "동", "리", "시", "군", "구")

# 용언·조사로 끝나면 이름이 아니다 — "걸어서 보건소", "가려고 보건소".
# 지명 판정을 **먼저** 하므로 '면'(하의면) 같은 겹침은 문제가 되지 않는다.
_VERB_ENDINGS = ("서", "고", "러", "며", "게", "다가", "는데", "니까", "지만",
                 "도록", "에", "까지", "부터", "처럼", "려고", "면서")


def detect_hospital(text: str) -> str | None:
    """발화에 직접 나온 병원 이름. 없으면 None.

    어르신이 이름을 댔으면 그것이 과거 이력보다 우선이다. 실통화에서
    "허리 아파서 내일 송정병원으로 10시에 가야 될 것 같아" 를 받고도 이력의
    다른 병원을 '확인됨' 으로 내놓은 적이 있다 — 직접 말한 것을 무시하면
    엉뚱한 곳으로 배차된다.
    """
    found: list[tuple[int, int, str]] = []
    for m in _HOSPITAL_RE.finditer(text):
        name, gap, suffix = m.group(1), m.group(2), m.group(3)
        if name in _NOT_A_NAME:
            continue
        # **띄어 썼으면 대개 상호가 아니다.**
        #
        # 차단 목록만으로는 감당이 안 됐다. "좋아서 병원", "맞으러 병원",
        # "타고 병원", "때문에 병원" — 한국어 어미를 다 셀 수는 없다. 실제로
        # 흔한 발화 24개 중 20개가 없는 병원을 만들어냈다.
        #
        # 진짜 상호는 붙여 쓴다(송정병원·목포한국병원). 띄어 쓴 것은
        # "목포 병원"(목포에 있는 아무 병원)처럼 설명인 경우가 훨씬 많다.
        # 다만 "보건소·의료원·대학병원" 같은 구체적인 꼬리와 "전남대학교 병원"
        # 은 띄어 써도 상호이므로 통과시킨다.
        if gap and suffix in _GENERIC_SUFFIX and not name.endswith(_NAME_ENDINGS):
            continue
        # 구체적인 꼬리(보건소·의료원·대학병원…)는 띄어 써도 통과시키는데,
        # 그 앞도 용언일 수 있다 — "걸어서 보건소". 지명이면 먼저 통과시키고
        # (하의면 보건지소), 그 다음에 용언 어미를 거른다.
        if (gap and not name.endswith(_PLACE_ENDINGS)
                and name.endswith(_VERB_ENDINGS)):
            continue
        # "내과 병원 가야 해" 는 특정 병원을 댄 것이 아니라 진료과를 말한 것이다.
        # 이걸 이름으로 잡으면 '확인됨' 으로 잘못 굳는다. 대신 "정형외과의원"
        # 같은 실제 상호는 놓치는데, 그건 이력 경로가 받아준다 — 놓치는 쪽이 낫다.
        if name in TERMS["dept_keywords"]:
            continue
        found.append((m.start(), m.end(), name + suffix))

    # 이름 뒤에서 부정하는 말이 오면 그 이름은 뺀다. 범위는 **다음 이름이
    # 시작되기 전까지** — "송정병원 말고 목포한국병원" 에서 뒤쪽까지 걸리면
    # 정정한 병원마저 사라진다.
    kept = []
    for i, (start, end, name) in enumerate(found):
        limit = found[i + 1][0] if i + 1 < len(found) else end + _NEGATION_WINDOW
        window = text[end:min(limit, end + _NEGATION_WINDOW)]
        if any(w in window for w in _NEGATED_AFTER):
            continue
        kept.append((start, end, name))
    # **다 빠지면 이름이 없는 것이 맞다.** 하나라도 남기려고 되돌리면 정작
    # 아니라고 말한 병원이 그대로 살아난다 — 그게 이 수정의 목적이었다.
    found = kept

    if not found:
        return None
    # 어르신은 말하면서 고친다 — "송정병원 말고 목포한국병원으로".
    # 날짜는 이미 정정을 처리하는데(dateparse) 병원만 첫 번째를 잡고 있었다.
    # 두 병원 사이에 정정하는 말이 있으면 **나중 것**이 최종이다.
    best = found[0][2]
    for (_, prev_end, _), (start, _, name) in zip(found, found[1:], strict=False):
        if _CORRECTION.search(text[prev_end:start]):
            best = name
    return best


# ---------------------------------------------------------------- 규칙 기반 ----

def _rule_based(text: str) -> Analysis:
    a = Analysis(raw=text)

    # 긴급 신호 — 가장 먼저, 보수적으로(조금이라도 의심되면 긴급)
    for kw in TERMS["urgent_keywords"]:
        if kw in text:
            a.urgent = True
            a.intent = "긴급"
            # **누가 쓰러졌는지는 긴급일수록 중요하다.**
            # 예전에는 여기서 바로 돌려보내 대리 판별이 아예 돌지 않았고,
            # "우리 어매가 쓰러졌어" 가 발신자 본인 이름으로 기록됐다.
            # 응급 기록에 엉뚱한 사람이 적히면 사람이 그걸 보고 움직인다.
            a.requester, a.proxy_relation = detect_proxy(text)
            return a

    # 진료과: 직접 언급 우선
    for dept in TERMS["dept_keywords"]:
        if dept in text:
            a.dept = dept
            a.dept_source = "spoken"
            break
    # 증상 → 진료과 (직접 언급이 없을 때)
    #
    # **먼저 나온 증상을 고른다.** 예전에는 사전에 적힌 순서대로 훑어 처음
    # 걸리는 것을 썼는데, 그러면 짧은 키가 긴 낱말 안에 박혀 있을 때 그쪽이
    # 이긴다. "발목이 다쳤어" 가 이비인후과로 갔다 — '발목이' 안의 '목이' 가
    # 먼저 걸렸기 때문이다. 손목·팔목도 같았다.
    #
    # 위치로 고르면 그 문제가 사라진다. '발목' 은 0 에서, '목이' 는 1 에서
    # 시작하므로 '발목' 이 이긴다. 같은 자리에서 시작하면 긴 쪽을 쓴다 —
    # 더 많이 설명하는 키다.
    #
    # 어느 증상을 먼저 말했는지로 고르는 것이기도 하다. 사전 편집 순서보다
    # 어르신이 말한 순서가 근거로 낫다.
    hits = [(text.index(sym), -len(sym), sym, dept)
            for sym, dept in TERMS["symptom_to_dept"].items() if sym in text]
    if hits:
        _, _, sym, dept = min(hits)
        a.symptom = sym
        if a.dept is None:
            a.dept = dept
            a.dept_source = "dict"

    # 사전이 못 잡으면 문장 임베딩으로 한 번 더 본다.
    #
    # **사전이 1차다.** 규칙이 걸리면 여기까지 오지 않는다 — 결정론이 우선이고
    # 임베딩은 사전을 무한히 키우지 않기 위한 보완이다. 어르신은 "손발이 저려",
    # "속이 울렁거려" 처럼 말하는데 그걸 전부 적어 두려다 한 글자 키가 늘고,
    # 그러다 '이'(치과)가 조사와 겹쳐 "우리 딸이 데려다 준대" 를 치과로 보냈다.
    #
    # 확신이 낮으면 비운다(deptmatch.THRESHOLD). 진료과는 게이트를 막지 않아
    # 비어도 접수는 진행되지만, 틀린 값이 들어가면 이력 필터를 바꿔 엉뚱한
    # 병원 후보가 나온다.
    if a.dept is None:
        try:
            from ..services import deptmatch
            got = deptmatch.guess(text)
        except Exception:                     # 모델이 없어도 접수는 계속된다
            got = None
        if got:
            a.dept, score = got
            a.dept_source = "embedding"
            a.notes.append(f"진료과는 발화 의미로 추정 — 유사도 {score:.2f}")

    # 의도: 약국 / 보호자연락 키워드
    for intent, kws in TERMS["intent_keywords"].items():
        if any(kw in text for kw in kws):
            a.intent = intent
            break

    # 대리 접수 판별 (긴급 다음으로 중요 — 대상자 확정에 영향)
    a.requester, a.proxy_relation = detect_proxy(text)

    # 발화에 직접 나온 병원명 — 이력보다 우선한다
    a.hospital = detect_hospital(text)

    # 날짜·시각(결정적)
    a.date = dateparse.parse_date(text)
    a.time = dateparse.parse_time(text)
    return a


# ----------------------------------------------------------------- LLM 보강 ----

def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _llm_refine(text: str, base: Analysis, client=None) -> Analysis:
    """Claude로 의도·진료과·증상·긴급신호를 보강. 날짜는 규칙 결과를 유지한다.

    client를 주입하면 키 없이도 이 경로를 실행·검증할 수 있다.
    """
    try:
        from pydantic import BaseModel
    except ImportError:
        base.notes.append("pydantic 미설치 — 규칙 기반만 사용")
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
    # anthropic 패키지는 클라이언트를 직접 만들 때만 필요하다.
    # 주입받은 경우까지 import 를 요구하면, 패키지 없는 환경(CI 등)에서
    # 테스트가 LLM 경로에 들어가지도 못하고 규칙 결과로 빠진다.
    if client is None:
        try:
            import anthropic
        except ImportError:
            base.notes.append("anthropic 미설치 — 규칙 기반만 사용")
            return base
        client = anthropic.Anthropic(timeout=settings.anthropic_timeout)

    try:
        resp = client.messages.parse(
            model=settings.anthropic_model,   # .env 의 ANTHROPIC_MODEL 을 따른다
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": f'발화: "{text}"'}],
            output_format=Parsed,
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            base.notes.append("LLM이 응답을 거부함 — 규칙 결과 사용")
            return base
        p = getattr(resp, "parsed_output", None)
        if p is None:
            base.notes.append("LLM 파싱 실패 — 규칙 결과 사용")
            return base
        # Analysis 를 새로 만들지 않고 base 를 복사해 덮어쓴다.
        # 새로 만들면 LLM 이 다루지 않는 필드(requester·proxy_relation 등)가
        # 조용히 기본값으로 되돌아간다 — 실제로 대리 전화 판별이 사라져
        # "어머니 병원 좀…" 이 본인 전화로 처리되던 버그가 있었다.
        # replace 를 쓰면 앞으로 필드가 늘어도 같은 사고가 나지 않는다.
        merged = replace(
            base,
            intent=p.intent if p.intent in INTENTS else base.intent,
            dept=p.dept or base.dept,
            symptom=p.symptom or base.symptom,
            hospital=base.hospital,         # 병원명도 규칙 결과 유지
            date=base.date,                 # 날짜·시각은 규칙 파서 결과 유지
            time=base.time,
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

def analyze(text: str, use_llm: bool | None = None, client=None) -> Analysis:
    base = _rule_based(text)
    if base.urgent:                 # 긴급이면 LLM 호출 없이 즉시 반환(빠른 사람 연결)
        return base
    if use_llm is None:
        use_llm = client is not None or _llm_available()
    if use_llm:
        return _llm_refine(text, base, client=client)
    return base
