"""요청 유형 판별 — 기존 흐름(이력→병원후보→날짜→상태→카드)이 감당할 수 있는
요청인지 **먼저** 가른다.

지금까지 어르신 발화는 무엇이 오든 '병원동행 접수'로 흘렀다. 그래서 이런 말이
들어와도 과거 단골이 '추정' 후보로 카드에 붙었다.

    "허리가 아픈데 주변에 어떤 병원이 있는지를 모르겠어"

어디로 갈지 몰라서 물어보는 사람에게 지난번 병원을 내미는 것이다. 게다가 우리는
어떤 병원이 있는지 **모른다** — 이력에 없는 병원은 만들어낼 수 없고, 만들어내면
그게 곧 지어내기다.

여기서는 **유형을 가르는 것까지만** 한다. 새 유형이면 병원 후보 산출과 진료과
추론을 끄고(pipeline), 조건만 구조화해 사회복지사에게 넘긴다.

## 규칙 기반인 이유

판단근거로 **원문 문구 그대로**를 돌려줘야 한다. 사회복지사가 "왜 이렇게
분류됐나"를 카드에서 바로 볼 수 있어야 하기 때문이다. 매칭된 문자열을 그대로
싣는 규칙 쪽이 정확하고, 외부 LLM 을 쓰지 않는다는 이 서비스의 전제와도
맞는다(AGENTS.md 7).

## 애매하면 '기존재방문'이다

탐색·인력 신호가 **뚜렷할 때만** 새 유형으로 본다. 조금이라도 애매하면 기존
흐름에 그대로 태운다 — 새 유형으로 잘못 보내면 접수카드가 아예 안 나오지만,
기존 흐름으로 가면 카드가 나오고 확정 게이트가 여전히 사람 확인을 요구한다.
잃는 것이 작은 쪽으로 기운다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import nlu as nlu_mod

# 기존 흐름을 그대로 쓰는 유형. 나머지 넷은 사회복지사 직접 응대로 간다.
DEFAULT = "기존재방문"

REQUEST_TYPES = ("기존재방문", "신규병원탐색", "진료과기반탐색",
                 "돌봄인력요청", "기타불분명")

# 새 유형 — 이 넷은 AI가 병원·진료과·인력을 채우지 않는다.
STAFF_HANDLED = ("신규병원탐색", "진료과기반탐색", "돌봄인력요청", "기타불분명")

# 카드·목록에 그대로 실리는 상태 문구.
STAFF_STATUS = "확인 필요 - 사회복지사 직접 응대 필요(신규요청)"

# 병원 후보를 조회해 볼 수 있는 유형. 돌봄인력요청·기타불분명은 조회 대상이 아니다.
LOOKUP_TYPES = ("신규병원탐색", "진료과기반탐색")


# ── 신호 사전 ────────────────────────────────────────────────────────────
#
# 전부 **원문에 그대로 있는 문자열**이다. 매칭된 문구를 근거로 그대로 싣기
# 때문에, 정규식으로 넓게 잡지 않고 실제 말투를 나열한다.

# 가 본 적 없는 병원을 찾는다는 신호
_NEW_HOSPITAL = (
    "새로 생긴", "새로 연", "새로 문 연", "새로 개원", "새 병원", "새병원",
    "처음 가", "가 본 적 없", "가본 적 없", "안 가 본", "안 가본",
    "다른 병원", "딴 병원", "병원을 옮기", "병원 옮기", "옮기고 싶", "바꾸고 싶",
)

# 어디에 무엇이 있는지 모른다는 신호. '병원'을 대상으로 물을 때만 잡는다 —
# "같이 가주실 분 있나요?" 의 '있나요' 까지 먹으면 평범한 동행 요청이 탐색
# 요청으로 넘어간다(파일3 회귀 케이스에 그 문장이 있다).
_SEARCH = (
    "어떤 병원", "무슨 병원", "어느 병원", "어디 병원", "어디로 가야",
    "어디를 가야", "어디가 좋", "어디가 나은",
    "병원이 있을까", "병원이 있나", "병원이 있는지", "병원 있을까", "병원 있나",
    "병원 있는지", "있는 병원", "병원이 어디", "병원 어디",
    "병원 좀 알아", "병원 알아봐", "병원 좀 찾", "병원 찾아", "병원 추천",
    "추천해 줄", "추천해줄", "추천 좀", "알아봐 주",
)

# 모른다 — 단독으로는 근거가 못 된다. 병원·진료과 이야기와 함께 있을 때만 센다.
_UNKNOWN = ("모르겠", "모르겄", "몰라서", "모르는데", "잘 몰라")

# 사람을 보내 달라는 신호.
#
# **'같이 가 줄 사람'은 여기 넣지 않는다.** 병원동행 서비스가 원래 사람이 같이
# 가는 것이라, 그 표현은 평범한 접수에서 가장 흔한 말이다. 여기서 가리려는 것은
# 병원 일정이 아니라 **사람 자체**를 요청하는 경우다.
_CARE_STAFF = (
    "사람 좀 보내", "사람을 보내", "사람 보내", "사람이 필요", "사람이 있어야",
    "누구 좀 보내", "누가 좀 와", "와 주실 분",
    "돌봐 줄 사람", "돌봐줄 사람", "돌봐 주실", "돌봐주실",
    "도와줄 사람", "도와 줄 사람", "봐 줄 사람", "봐줄 사람",
    "돌봄", "간병", "요양보호사", "생활지원사", "인력",
    "혼자서는 못", "혼자 못 가", "혼자 갈 수가 없", "혼자서 못",
)

# 막연한 도움 요청. 대상(병원·진료과·인력)이 하나도 안 잡힐 때만 '기타불분명'.
_HELP = ("어떻게 해야", "어떡하", "어쩌면 좋", "방법이 없을까", "상담 좀",
         "여쭤보려", "물어보려", "도움이 필요")

# 위치 조건. 긴 것부터 본다 — "우리 집 주변"을 "주변"으로 자르면 근거가 준다.
_LOCATION = ("우리 집 주변", "우리집 주변", "집 주변", "집 근처", "우리 동네",
             "가까운 데", "가까운 곳", "동네", "근처", "주변", "가까운", "가까이",
             "걸어서", "면 소재지", "읍내", "시내")

# 방문 계획이 이미 서 있다는 신호 — 이게 있으면 탐색·인력으로 보지 않는다.
_VISIT_VERB = ("가야", "가려", "갈라", "가자", "가기로", "예약", "진료 받", "진료받",
               "가 보려", "가봐야", "모시고 가", "델꼬 가", "데리고 가")


@dataclass
class RequestType:
    """유형 하나 + 그렇게 본 근거(원문 문구) + 구조화된 조건."""

    type: str
    evidence: list[str] = field(default_factory=list)
    conditions: dict = field(default_factory=dict)

    @property
    def staff_handled(self) -> bool:
        return self.type in STAFF_HANDLED

    def summary(self) -> str:
        """카드 '요청 내용' 칸에 넣을 한 줄. 조건은 원문에서 뽑은 것만 들어간다."""
        parts = [self.type]
        for key in ("원하는진료과", "위치조건", "사유", "필요한도움"):
            v = self.conditions.get(key)
            if v:
                parts.append(f"{key} {v}")
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {"type": self.type, "evidence": self.evidence,
                "conditions": self.conditions, "staff_handled": self.staff_handled}


def _hits(text: str, words) -> list[str]:
    """원문에 실제로 있는 문구만 골라 **긴 것부터** 돌려준다.

    돌려주는 문자열이 곧 카드에 실리는 근거다. 원문에 없는 말이 섞이면
    "왜 이렇게 분류됐나"에 거짓으로 답하게 된다.
    """
    found = [w for w in words if w in text]
    found.sort(key=len, reverse=True)
    return found


def _spoken_dept(text: str) -> str | None:
    """어르신이 **직접 말한** 진료과. 증상에서 우리가 추정한 것은 여기 안 들어온다."""
    for dept in nlu_mod.TERMS["dept_keywords"]:
        if dept in text:
            return dept
    return None


def _symptom(text: str) -> str | None:
    """증상 표현 — 원문에 있는 낱말 그대로. 진료과로 옮기지 않는다."""
    hits = [(text.index(sym), -len(sym), sym)
            for sym in nlu_mod.TERMS["symptom_to_dept"] if sym in text]
    return min(hits)[2] if hits else None


def classify(utterance: str, analysis=None) -> RequestType:
    """발화 하나를 다섯 유형 중 하나로 가른다.

    analysis 는 있으면 쓰고 없으면 이 안에서 필요한 만큼만 다시 본다 — 이 함수
    하나만 떼어 Tool 로 부를 수 있어야 하기 때문이다(입력은 발화 텍스트뿐).

    **DB·프로필·이력을 보지 않는다.** 발화만으로 판단하므로 "이 사람에게 이력이
    있는가"는 여기서 묻지 않는다. 그건 유형이 정해진 뒤 기존 흐름이 하는 일이다.
    """
    text = utterance or ""
    spoken_dept = (analysis.dept if analysis is not None
                   and getattr(analysis, "dept_source", None) == "spoken"
                   else _spoken_dept(text))
    symptom = (analysis.symptom if analysis is not None and analysis.symptom
               else _symptom(text))
    spoken_hospital = analysis.hospital if analysis is not None else None

    loc = _hits(text, _LOCATION)
    base: dict = {}
    if loc:
        base["위치조건"] = loc[0]
    if symptom:
        base["사유"] = symptom

    # 이미 갈 곳·갈 일이 정해진 말인가. 병원 이름을 댔거나, 진료과를 직접 말하며
    # 가겠다고 한 경우다. 그러면 탐색·인력 신호가 섞여 있어도 기존 흐름이 맞다 —
    # "모레 정형외과 가야하는데, 같이 가주실 분 있나요?" 가 그런 문장이다.
    has_plan = bool(spoken_hospital) or (bool(spoken_dept)
                                         and any(v in text for v in _VISIT_VERB))

    # ① 사람을 보내 달라 — 병원 일정이 아니라 사람 자체를 요청한 경우
    care = _hits(text, _CARE_STAFF)
    if care and not has_plan:
        base["필요한도움"] = care[0]
        return RequestType("돌봄인력요청", _evidence(care, loc, symptom),
                           _typed(base, "돌봄인력요청", None))

    # ② 어디로 가야 할지 모른다 — 새 병원 탐색
    search = _hits(text, _NEW_HOSPITAL) + _hits(text, _SEARCH)
    unknown = _hits(text, _UNKNOWN)
    # '모르겠다'는 병원 이야기와 함께 있을 때만 근거로 센다.
    if unknown and ("병원" in text or "의원" in text or spoken_dept):
        search = search + unknown
    if search and not has_plan:
        kind = "진료과기반탐색" if spoken_dept else "신규병원탐색"
        return RequestType(kind, _evidence(search, loc, symptom),
                           _typed(base, kind, spoken_dept))

    # ③ 대상이 하나도 안 잡히는 막연한 도움 요청
    help_hits = _hits(text, _HELP) + (unknown if not search else [])
    if help_hits and not (spoken_dept or symptom or spoken_hospital
                          or "병원" in text or care):
        return RequestType("기타불분명", _evidence(help_hits, loc, symptom),
                           _typed(base, "기타불분명", None))

    # ④ 그 외는 전부 기존 흐름. 애매하면 이쪽이다.
    return RequestType(DEFAULT, [], {"요청유형": DEFAULT})


def _evidence(hits: list[str], loc: list[str], symptom: str | None) -> list[str]:
    """근거는 **원문에 있던 문구**만 싣는다. 요약하거나 바꿔 적지 않는다."""
    out = [f"원문 '{h}'" for h in hits[:3]]
    if loc:
        out.append(f"위치 조건 '{loc[0]}'")
    if symptom:
        out.append(f"증상 표현 '{symptom}'")
    return out


def _typed(base: dict, kind: str, spoken_dept: str | None) -> dict:
    """구조화 결과. **어르신이 직접 말한 것만** 담는다.

    증상에서 우리가 추정한 진료과("다리가 불편" → 정형외과)는 넣지 않는다.
    그건 우리 판단이지 어르신이 말한 조건이 아니고, 조건으로 굳으면 그 값으로
    병원을 찾게 된다.
    """
    out = {"요청유형": kind}
    if spoken_dept:
        out["원하는진료과"] = spoken_dept
    out.update(base)
    return out
