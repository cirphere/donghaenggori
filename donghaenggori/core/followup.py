"""통화 중 후속질문 — 확인 필요로 남은 칸을 **어르신이 아직 통화 중일 때** 한 번 더 묻는다.

지금까지는 카드에 '확인 필요'가 남으면 그대로 끊고, 사회복지사가 나중에 되걸었다.
어르신은 이미 전화기를 들고 있는데 우리가 물어보지 않은 것이다.

## 왜 이제 물어도 되나

이 저장소는 통화에 질문을 더하는 것을 한 번 되돌린 적이 있다(#105, 통화 앞의 본인
확인). 뺀 이유가 코드에 남아 있다 — **그 답이 접수 카드를 바꾸지 않았기 때문**이다.
1번을 눌러도 안 눌러도 대상자는 '확인됨'이고 게이트도 똑같이 열렸다.

그래서 기준은 "질문을 늘리지 말 것"이 아니라 **"카드를 바꾸는 질문만 할 것"**이다.
여기서 묻는 것은 게이트를 실제로 막고 있는 칸이고, 답을 받으면 값이 채워져 게이트가
열린다. 그 기준을 통과한다.

## 무엇을 묻나 — 게이트가 막는 칸만

`gate.blockers`가 이미 "무엇이 막는가 + 무엇을 물으면 되는가"를 돌려준다.
**질문을 여기서 새로 만들지 않는다.** 두 곳에서 각자 만들면 화면이 띄운 질문과
통화가 묻는 질문이 갈라지고, 그때부터 어느 쪽이 맞는지 아무도 모른다.

`target`은 묻지 않는다. 8kHz 전화 음질에서 받아 적은 이름은 **어떤 경로로도
'확인됨'이 되지 않기 때문이다**(card.fields_view 참조). 물어봐야 게이트가 안 열리고
통화만 길어진다. 대상자는 사회복지사가 원문을 듣고 정한다.

## 안 하는 것

- 한 번에 두 칸을 묻지 않는다. 어르신이 무엇에 답한 것인지 우리가 알 수 없게 된다
- `classify_confidence`(= pipeline._field_status)의 판정 로직을 건드리지 않는다.
  **이미 '확인 필요'로 분류된 칸에 대해서만** 되묻는 보조 기능이다
- 신규 유형 요청(requesttype.STAFF_HANDLED)에는 적용하지 않는다. 그건 되물어서
  풀리는 종류가 아니라 사람이 응대할 요청이다
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

from . import dateparse
from . import nlu as nlu_mod
from . import requesttype as rt_mod
from .korean import particle

# 통화당 후속질문 상한. 0 이면 기능이 꺼진다 — 시연장에서 한 줄로 끌 수 있어야 한다.
#
# 동행 정보의 기본 항목(병원·방문일·시각·진료과·대상자)이 빠지면 복지사가 결국
# 다시 전화한다. 어르신이 아직 통화 중일 때 받는 편이 낫다는 판단으로 넷까지
# 늘렸다. 무응답·혼란이 이어지면 그 전에 사람에게 넘어가므로(detect_handoff_signal)
# 통화가 무한히 늘어나지는 않는다.
DEFAULT_MAX_QUESTIONS = 4

# 되물을 항목과 **묻는 순서**.
#
# **gate.BLOCKING 과 다른 목록이다.** 막는 기준은 "일정을 세우는 데 반드시
# 필요한가" 이고, 묻는 기준은 "동행 정보에서 빠지면 다시 전화해야 하는가" 다.
# 진료과는 없어도 확정할 수 있지만(막지 않는다) 통화에서는 물어본다.
#
# 순서는 게이트가 막는 것부터다 — 통화가 중간에 끊겨도 확정에 필요한 것이 먼저
# 채워지는 편이 낫다.
ASK_ORDER = ("hospital", "date", "time", "dept", "target")

# 예전 이름. 되물을 항목이 셋뿐이던 시절 이 이름으로 참조했다.
ASKABLE = ASK_ORDER


# ── 후속질문 생성 ────────────────────────────────────────────────────────

# 통화용으로 다듬을 때 지우는 군더더기. 화면 질문은 복지사가 읽는 것이라 조금
# 길어도 되지만, 통화에서는 문장이 길수록 어르신이 끝까지 듣지 않는다.
_TRIM = (
    ("어르신, ", ""),
    (" 확인 부탁드립니다", " 말씀해 주세요"),
    ("한 번 더 확인 부탁드립니다", "다시 한 번 말씀해 주세요"),
    # 화면용 문구가 통화에서는 답을 유도하지 못하는 경우. 뜻은 그대로 두고
    # 어르신이 답할 수 있는 말로 바꾼다.
    ("방문 시각도 알려주시면 차량 배차에 반영하겠습니다.",
     "몇 시에 가시는지 알려 주시겠어요?"),
    ("성함과 거주 읍면동을 알려주시면 대상자를 확인하겠습니다.",
     "성함을 다시 한 번 말씀해 주시겠어요?"),
)


@dataclass
class Followup:
    field: str
    question: str
    label: str = ""

    def to_dict(self) -> dict:
        return {"field": self.field, "question": self.question, "label": self.label}


def _for_call(question: str) -> str:
    """화면용 질문을 통화용 한 문장으로 다듬는다. 뜻은 바꾸지 않는다."""
    out = question.strip()
    for a, b in _TRIM:
        out = out.replace(a, b)
    # 두 문장이면 앞의 하나만 쓴다 — 한 번에 하나만 묻는다는 규칙이 문장 수에도 걸린다.
    parts = [p for p in re.split(r"(?<=[?？])\s+", out) if p.strip()]
    return parts[0].strip() if parts else out


def generate_followup_question(field: str, known: dict,
                               asked: tuple[str, ...] = ()) -> Followup | None:
    """확인 필요로 남은 칸 하나를 묻는 질문. 물을 것이 없으면 None.

    known 은 지금까지 파악한 내용 — 접수카드 dict 다. **이미 아는 것은 묻지
    않는다**: 상태가 '확인 필요' 인 칸만 대상이고, 값이 있으면 애초에 그 상태가
    아니다.
    """
    from . import gate  # 순환 import 를 피해 함수 안에서 부른다

    if field in asked or field not in ASK_ORDER:
        return None
    f = ((known or {}).get("fields") or {}).get(field)
    if not f or f.get("status") != "확인 필요":
        return None
    q = gate.question_for(field, known)
    if not q:
        return None                          # 물을 말을 지어내지 않는다
    return Followup(field=field, question=_for_call(q), label=f.get("label") or field)


def pending_fields(card: dict | None, asked: tuple[str, ...] = ()) -> list[str]:
    """되물을 항목을 ASK_ORDER 순서로. 이미 물은 것은 뺀다.

    **게이트가 막는 목록을 그대로 쓰지 않는다.** 진료과는 확정을 막지 않지만
    (없어도 동행은 나간다) 동행 정보에서 빠지면 복지사가 다시 전화하게 되므로
    통화에서는 묻는다. 시각도 어르신이 말한 적 없어도 묻는다 — 게이트는 그때
    막지 않지만(OPTIONAL_UNLESS_SPOKEN), 예약 시간 없이 동행을 잡을 수는 없다.

    막는 것과 묻는 것을 가른 것이다. 되물어서 못 얻으면 그 칸은 '확인 필요' 로
    남고, 확정 정책은 지금까지와 똑같다.
    """
    if not card:
        return []
    # 신규 유형은 되물어서 풀리지 않는다 — 통째로 사람에게 넘긴다.
    if (card.get("request_type") or "") in rt_mod.STAFF_HANDLED:
        return []
    fields = card.get("fields") or {}
    return [name for name in ASK_ORDER
            if name not in asked
            and (fields.get(name) or {}).get("status") == "확인 필요"]


def next_question(card: dict | None, asked: tuple[str, ...] = ()) -> Followup | None:
    """다음에 물을 것 하나. **한 번에 하나만** 돌려준다."""
    for f in pending_fields(card, asked):
        q = generate_followup_question(f, card, asked)
        if q:
            return q
    return None


# ── 사람 연결 신호 ───────────────────────────────────────────────────────

# 사람을 **직접 요청**한 말. 이건 담당자에게 돌린다.
_EXPLICIT = ("사람 바꿔", "사람좀 바꿔", "사람 좀 바꿔", "사람하고", "사람이랑",
             "직원", "담당자", "복지사", "상담원", "선생님 바꿔", "사람 연결")

# 그만하겠다는 말. 캐묻지 않는다.
_REFUSAL = ("됐어", "됐다", "됐구요", "됐고", "그만", "관두", "안 할래", "안할래",
            "필요 없", "필요없", "끊을게", "끊을래", "나중에")

# 못 알아들었다는 말.
_CONFUSED = ("모르겠", "모르겄", "몰라", "무슨 말", "뭔 말", "뭐라고", "못 알아",
             "안 들려", "잘 안 들")

# 답이라고 보기 어려운 짧은 말. 이것만 오면 '불명확'으로 센다.
_EMPTY_ANSWER = ("", "네", "예", "어", "음", "글쎄", "그냥", "아", "응")

# 같은 답이 반복되거나 불명확한 답이 이어지면 사람에게 넘긴다.
UNCLEAR_LIMIT = 2


@dataclass
class Handoff:
    needed: bool
    reason: str = ""
    explicit: bool = False          # 사람을 직접 요청했는가 (담당자 전환 대상)

    def to_dict(self) -> dict:
        return {"handoff_필요": self.needed, "근거": self.reason, "직접요청": self.explicit}


@dataclass
class CallState:
    """통화 하나가 들고 있는 것. **DB 에 넣지 않는다.**

    접수로 이어지지 못한 통화(중간에 끊김)의 발화를 남기지 않기 위해서다 —
    voice._IDENTITY_SAID 와 같은 이유이고, 접수가 저장될 때만 카드에 기록한다.
    """

    asked: list[str] = dc_field(default_factory=list)
    answers: list[str] = dc_field(default_factory=list)
    unclear: int = 0                # 연달아 불명확했던 횟수

    def record(self, field: str, answer: str, clear: bool) -> None:
        self.asked.append(field)
        self.answers.append(answer)
        self.unclear = 0 if clear else self.unclear + 1


def _norm(text: str) -> str:
    return re.sub(r"[\s.,!?~]+", "", (text or "")).strip()


def detect_handoff_signal(utterance: str, state: CallState | None = None) -> Handoff:
    """혼란·거부·사람 요청 신호. **재추출보다 먼저 본다.**

    되묻는 도중에 "됐어요", "사람 바꿔줘"가 나오면 그 말이 답변보다 우선이다.
    한 번 더 캐묻는 것이 이 통화에서 제일 하면 안 되는 일이다.

    한 마디만으로도 판단하지만, '무응답·불명확이 이어지는 것'은 한 마디로는
    알 수 없어 통화 상태(state)를 함께 본다.
    """
    text = (utterance or "").strip()
    norm = _norm(text)

    for w in _EXPLICIT:
        if _norm(w) in norm:
            return Handoff(True, f"사람을 직접 요청함 — '{w}'", explicit=True)
    for w in _REFUSAL:
        if _norm(w) in norm:
            return Handoff(True, f"그만하겠다는 응답 — '{w}'")
    for w in _CONFUSED:
        if _norm(w) in norm:
            return Handoff(True, f"질문을 이해하지 못함 — '{w}'")
    # 다른 병원을 찾아 달라는 답. **되묻기로 풀리는 종류가 아니다** — 우리는
    # 병원을 추천하지 않으므로(신규 병원 탐색은 사회복지사 몫) 여기서 끝낸다.
    # 캐물어 봐야 어르신은 우리가 못 하는 것을 계속 기다린다.
    for w in _WANT_OTHER:
        if _norm(w) in norm:
            return Handoff(True, f"다른 병원을 찾아 달라는 요청 — '{w}'")

    if state is not None:
        # 같은 말을 되풀이한다 — 우리 질문이 닿지 않고 있다는 뜻이다.
        if norm and state.answers and _norm(state.answers[-1]) == norm:
            return Handoff(True, f"같은 답을 반복함 — '{text}'")
        if state.unclear >= UNCLEAR_LIMIT:
            return Handoff(True, f"답을 얻지 못한 질문이 {state.unclear}회 이어짐")

    return Handoff(False)


# 한글 음절. 후속답변에 이것이 하나도 없으면 어르신의 말이 아니다.
_HANGUL = re.compile(r"[가-힣]")


def is_unclear(answer: str) -> bool:
    """답이라고 보기 어려운가. 무응답·전사 실패도 여기로 온다.

    **한글이 한 글자도 없으면 답으로 보지 않는다.**

    후속답변은 15초짜리 짧은 녹음이라 어르신이 말을 안 하면 무음이 길고,
    Whisper 는 무음에서 다른 언어를 지어낸다. 실통화에서 이런 것이 답변
    자리에 들어왔다.

        어느 병원으로 모실지 말씀해 주세요.
        私はもう生まれます。                    ← 어르신의 말이 아니다

    복지사 화면에 이것이 '어르신 답' 으로 뜨면, 그 통화에서 무슨 일이
    있었는지 잘못 읽는다. 재추출로 넘어가 이 문장에서 병원 이름을 찾는
    것은 더 나쁘다.

    한국 어르신이 한국 복지 서비스에 건 전화다. 한글이 없으면 전사 실패로
    본다 — 숫자만 있는 답("3시")은 원문에 붙여 쓰이므로 여기서 걸러도
    잃는 것이 없다. 실제로 '3시' 같은 답은 조사가 붙어 한글이 섞인다.
    """
    text = (answer or "").strip()
    if not text:
        return True
    if not _HANGUL.search(text):
        return True
    return _norm(answer) in {_norm(w) for w in _EMPTY_ANSWER}


# ── 필드 재추출 ──────────────────────────────────────────────────────────

# 후보를 되물었을 때의 긍정. "지난번 가셨던 ○○병원 맞으실까요?" → "응 맞아"
_YES = ("맞아", "맞어", "맞습니다", "맞네", "그래", "그려", "그렇지", "응", "예", "네",
        "그거", "거기", "그 병원", "그럼")
_NO = ("아니", "아녀", "아니요", "아뇨", "안 가", "틀려", "없어", "없으니", "없는데",
       "없대", "없다", "안 해", "안 봐", "못 봐")

# **다른 병원을 찾아 달라는 답.** 되묻던 흐름을 여기서 끝내고 사람에게 넘긴다 —
# 우리는 병원을 추천하지 않는다(신규 병원 탐색은 사회복지사 몫이다).
#
# 실통화에서 이걸 놓쳤다. "백병원에는 피부과가 없으니 다른 병원을 추천해 주세요"
# 를 받고도 백병원을 확인됨으로 채워 "백병원으로 접수했습니다" 를 들려줬다.
_WANT_OTHER = ("다른 병원", "딴 병원", "다른 데", "딴 데", "다른 곳", "추천",
               "찾아 주", "찾아주", "알아봐 주", "알아봐주", "어디 좋", "어디가 좋")

# 오전·오후만 답한 경우. "3시"는 원문에 있으니 거기에 붙인다.
_MERIDIEM = ("오전", "오후", "새벽", "아침", "점심", "저녁", "밤", "낮")


@dataclass
class Reextract:
    field: str
    value: str | None = None
    status: str = "확인 필요"          # 확인됨 | 추정 | 확인 필요
    evidence: list[str] = dc_field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.value is not None and self.status != "확인 필요"

    def to_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "status": self.status,
                "evidence": self.evidence}


def reextract_field(field: str, original: str, answer: str,
                    card: dict | None = None, question: str = "") -> Reextract:
    """후속답변을 반영해 **그 칸 하나만** 다시 뽑는다. 실패하면 '확인 필요' 그대로.

    original(원발화)이 필요한 이유는 시각 때문이다. "3시"에 "오후요"라고 답하면
    답변만으로는 시각이 안 나온다 — 원문의 '3시'에 붙여야 15:00 이 된다.

    **다른 칸은 건드리지 않는다.** 답변에 병원 이름이 섞여 있어도 날짜를 묻던
    중이면 날짜만 본다. 어르신이 무엇에 답한 것인지는 우리가 물은 것으로만 안다.
    """
    fields = (card or {}).get("fields") or {}
    spoken = (fields.get(field) or {}).get("spoken")
    said = (answer or "").strip()
    if not said:
        return Reextract(field, evidence=["후속질문에 답변이 없었음 — 확인 필요 유지"])

    if field == "hospital":
        return _reextract_hospital(said, card, question)
    if field == "date":
        return _reextract_date(said)
    if field == "time":
        return _reextract_time(said, spoken)
    if field == "dept":
        return _reextract_dept(said)
    if field == "target":
        return _reextract_target(said)
    # 되물을 수 없는 칸이 여기 오면 아무것도 하지 않는다.
    return Reextract(field, evidence=[f"'{field}' 은 통화로 되묻지 않는 항목"])


def _reextract_hospital(said: str, card: dict | None, question: str = "") -> Reextract:
    """**부정·탐색 요청을 이름 추출보다 먼저 본다.**

    순서가 뒤집혀 있어서 실통화가 깨졌다. "백병원에는 피부과가 없으니 다른
    병원을 추천해 주세요" 에서 병원명 추출이 먼저 돌아 '백병원' 을 확인됨으로
    올렸고, 통화 마지막에 "백병원으로 접수했습니다" 가 나갔다. 어르신은 그
    병원이 **아니라고** 말한 것이다.
    """
    norm = _norm(said)
    candidate = _asked_candidate(card, question)

    # ① 다른 병원을 찾아 달라 — 우리가 채울 수 있는 답이 아니다.
    if any(_norm(w) in norm for w in _WANT_OTHER):
        return Reextract("hospital", None, "확인 필요",
                         [f"후속질문에 '{said}'라고 답함 — 다른 병원을 찾아 달라는 요청",
                          "AI가 병원을 추천하지 않는다 — 사회복지사 직접 응대 필요"])

    # ② 아니라는 말이 섞여 있으면 그 말이 이긴다. 후보를 되물었든 아니든 같다 —
    #    "거기 아니에요" 의 '거기' 가 동의로 잡히던 것도 이 순서로 막는다.
    #    틀린 병원으로 배차하는 것보다 한 번 더 확인하는 쪽이 싸다.
    if any(_norm(w) in norm for w in _NO):
        why = f" — {candidate} 아님" if candidate else ""
        return Reextract("hospital", None, "확인 필요",
                         [f"후속질문에 '{said}'라고 답함{why}"])

    # ③ 이름을 직접 댔다 — 기존 규칙과 같은 대우다(hospital.suggest).
    name = nlu_mod.detect_hospital(said)
    if name:
        return Reextract("hospital", name, "확인됨",
                         [f"후속질문에 '{name}'{_particle(name)} 직접 말함"])

    if candidate and any(_norm(w) in norm for w in _YES):
        # 후보를 되물어 "맞다"고 답했다. **확인됨은 주지 않는다** — 8kHz 전사에서
        # '네'와 '아니요'가 뒤집히는 일이 있고, 이 값은 배차까지 흘러간다.
        # 근거를 대고 고른 값이므로 '추정'이고, 확정은 사회복지사가 한다.
        return Reextract("hospital", candidate, "추정",
                         [f"후속질문 '{candidate} 맞으실까요'에 '{said}'라고 답함",
                          "자동 전사 결과라 확인됨으로 올리지 않음 — 확정 전 사회복지사 확인"])
    return Reextract("hospital", None, "확인 필요",
                     [f"후속답변 '{said}' 에서 병원 이름을 찾지 못함"])


def _reextract_date(said: str) -> Reextract:
    d = dateparse.parse_date(said)
    if d and d.get("date") and d.get("confident"):
        return Reextract("date", d["date"], "확인됨",
                         [f"후속질문에 '{d.get('label') or said}'{_particle(d.get('label') or said)}"
                          " 직접 말함"])
    if d and d.get("label"):
        # 들리기는 했는데 확정이 안 된다("10일이나 11일"). 고르지 않는다.
        return Reextract("date", None, "확인 필요",
                         [f"후속답변에서 '{d['label']}'{_particle(d['label'])} 들었으나 확정할 수 없음"])
    return Reextract("date", None, "확인 필요",
                     [f"후속답변 '{said}' 에서 날짜를 찾지 못함"])


def _reextract_time(said: str, spoken: str | None) -> Reextract:
    t = dateparse.parse_time(said)
    if t and t.get("time") and t.get("confident"):
        return Reextract("time", t["time"], "확인됨",
                         [f"후속질문에 '{t.get('label') or said}'{_particle(t.get('label') or said)}"
                          " 직접 말함"])
    # 오전·오후만 답한 경우 — 원문에서 들은 시각에 붙인다("3시" + "오후").
    mark = next((m for m in _MERIDIEM if m in said), None)
    if mark and spoken:
        t2 = dateparse.parse_time(f"{mark} {spoken}")
        if t2 and t2.get("time") and t2.get("confident"):
            return Reextract("time", t2["time"], "확인됨",
                             [f"어르신이 '{spoken}'이라 말한 시각에 후속질문 답변 '{mark}'을 적용"])
    return Reextract("time", None, "확인 필요",
                     [f"후속답변 '{said}' 로는 시각을 확정할 수 없음"])


# 후보를 되묻는 질문의 모양. pipeline 이 만드는 문장이 이렇다 —
#   "어르신, 지난번 가셨던 ○○정형외과의원 맞으실까요?"
_CANDIDATE_RE = re.compile(r"가셨던\s+(.+?)\s*맞으실까요")


def _reextract_dept(said: str) -> Reextract:
    """진료과 — 어르신이 **말한 것만** 쓴다.

    증상에서 우리가 옮기지 않는다("허리가 아파요" → 정형외과). 그건 되묻기 전에도
    할 수 있었던 추정이고, 되물은 자리에서 추정을 답으로 채우면 물어본 의미가 없다.
    어느 과인지 모르는 어르신이 많고, 그때는 확인 필요로 남는 것이 맞다.
    """
    for dept in nlu_mod.TERMS["dept_keywords"]:
        if dept in said:
            return Reextract("dept", dept, "확인됨",
                             [f"후속질문에 '{dept}'{_particle(dept)} 직접 말함"])
    return Reextract("dept", None, "확인 필요",
                     [f"후속답변 '{said}' 에서 진료과를 확인하지 못함",
                      "증상에서 진료과를 옮기지 않는다 — 사회복지사가 확인"])


def _reextract_target(said: str) -> Reextract:
    """대상자 — **들은 것을 기록하고 값은 채우지 않는다.**

    8kHz 전화 음질에서 받아 적은 이름은 어떤 경로로도 '확인됨' 이 되지 않는다
    (card.fields_view). '추정' 을 주면 게이트가 풀리는데, 대상자는 발신번호로도
    확정하지 않는다는 것이 이 서비스의 불변조건이다(AGENTS.md 3).

    그래서 여기서는 묻고 **답을 그대로 남기는 것까지만** 한다. 복지사는 카드의
    후속질문 기록에서 어르신이 말한 성함을 읽고, 확정은 사람이 한다. 물어본 것이
    헛일은 아니다 — 되걸어 "성함이 어떻게 되세요" 를 다시 묻지 않아도 된다.
    """
    from . import identity as identity_mod

    name, region = identity_mod.parse_identity_answer(said)
    heard = " · ".join(x for x in (name, region) if x)
    if heard:
        return Reextract("target", None, "확인 필요",
                         [f"통화에서 들은 것 — {heard}",
                          "이름은 전화 음질에서 오인식이 잦다 — 대상자 확정은 사회복지사가 한다"])
    return Reextract("target", None, "확인 필요",
                     [f"후속답변 '{said}' 에서 성함을 확인하지 못함"])


def _asked_candidate(card: dict | None, question: str = "") -> str | None:
    """"맞다"가 무엇을 가리키는가 — **우리가 물은 질문에 들어 있던 이름**이다.

    카드의 hospital 을 그냥 쓰지 않는다. 후보 없이 "어느 병원으로 모실지
    말씀해 주세요"를 물었을 수도 있는데, 그 질문에 "네"라고 답한 것을 무언가에
    대한 동의로 읽으면 안 된다.

    이름 추출에 detect_hospital 을 쓰지 않는다. 그쪽은 '병원·의원' 같은 꼬리를
    보는데 이력의 상호는 '○○정형외과' 처럼 진료과로 끝나는 경우가 흔해서, 정작
    우리가 물어본 이름을 못 잡는다. 여기서는 **우리가 만든 문장**을 읽는 것이라
    자리로 찾는 편이 정확하다.
    """
    m = _CANDIDATE_RE.search(question or "")
    if m:
        return m.group(1).strip() or None
    return nlu_mod.detect_hospital(question or "") or None


def _particle(word: str | None) -> str:
    return particle(word or "", "을")


def max_questions(raw: str | None) -> int:
    """통화당 상한. 설정이 이상하면 기본값으로 돌아간다(통화를 막지 않는다)."""
    try:
        n = int((raw or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_QUESTIONS
    # 되물을 항목이 다섯이라 상한도 거기까지 허용한다. 그보다 크게 잡을 이유가
    # 없다 — 물을 것이 없으면 알아서 멈춘다.
    return max(0, min(n, 5))
