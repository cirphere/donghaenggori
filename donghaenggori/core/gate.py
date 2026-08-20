"""접수 확정 게이트 — "AI는 후보·근거까지, 확정은 사람"을 실제로 막아서 지킨다.

지금까지 확정은 무조건 통과였다. 카드에 '확인 필요'가 남아 있어도 버튼만
누르면 일정이 잡혔다. 그러면 화면의 상태 배지는 장식이고, 잘못 들은 병원으로
어르신이 헛걸음한다.

여기서는 **막는 것까지만** 한다. 무엇이 왜 막는지와 어떻게 물어보면 되는지를
구조로 돌려주고, 화면이 그걸 그린다. 화면이 판단을 흉내 내면 화면마다 규칙이
갈라지므로, 규칙은 서버 한 곳에만 둔다.
"""
from __future__ import annotations

import os

# 확정을 막는 항목. 여기 없는 항목(진료과)은 비어 있어도 확정할 수 있다.
#
# 기준은 하나다 — **일정을 세우는 데 반드시 필요한가.** 누구를(대상자),
# 어디로(병원), 언제(방문일·시각)가 없으면 일정 자체가 성립하지 않는다.
# 진료과는 몰라도 동행은 나간다.
#
# 말한 성함·말한 주소는 일부러 넣지 않았다. 그 둘은 대상자를 알아내려고 통화에서
# 받아 적은 단서일 뿐, 그 자체가 확정해야 할 항목이 아니다. 항목으로 세우면
# 사회복지사가 전화 한 번으로 끝낼 "이분이 누구신가"가 화면에서 세 칸으로
# 쪼개진다. 대신 target 블로커가 그 둘을 '들은 말'로 함께 들고 간다.
#
# 'request'(요청 내용)는 기존 흐름이 감당하지 못하는 요청에서만 세워지는 칸이다
# (card.REQUEST_TYPE_FIELDS). 이 칸이 없으면 새 유형 카드가 **조용히 통과한다** —
# 병원·날짜 칸을 세우지 않았으니 막을 것이 없어서 allowed=true 가 되고, 사람이
# 응대하지 않은 요청이 확정으로 넘어간다. 유형을 가르는 일이 오히려 게이트를
# 푸는 결과가 되므로, 새 유형에는 반드시 막는 칸이 하나 있어야 한다.
BLOCKING = ("target", "request", "hospital", "date", "time")

# 어르신이 말한 적조차 없으면 막지 않는 항목.
#
# 시각은 없어도 접수를 막지 않는다는 정책이 이미 있다(pipeline._make_card 주석).
# 날짜 없는 일정은 못 잡지만 시각 없는 일정은 "시간 미정"으로 잡힌다. 다만
# **말했는데 모호한** "3시"는 막는다 — 오전·오후를 우리가 고르면 절반의 확률로
# 어르신이 반나절을 헛걸음한다. 없는 것보다 틀린 것이 나쁘다.
#
# 그 둘을 가르는 건 value 가 아니라 spoken 이다. 모호한 "3시"는 해석에 실패해
# value 가 None 인 채 spoken 에만 남는다 — value 로 갈랐더니 정작 막아야 할
# 쪽이 "값 없음"으로 분류돼 통과했다.
OPTIONAL_UNLESS_SPOKEN = ("time",)

# 대상자 블로커에 '들은 말'로 따라붙는 항목.
HEARD = ("spoken_name", "spoken_region")


def blockers(card: dict | None) -> list[dict]:
    """확정을 막는 항목들 — 화면이 그대로 그릴 수 있는 형태로.

    '추정'은 막지 않는다. 상태를 3단계로 나눈 이유가 여기 있다 — 추정은 "근거를
    대고 고른 값"이고 확인 필요는 "모른다"다. 추정까지 막으면 거의 모든 카드가
    걸려서 게이트가 잡음이 되고, 3단계는 사실상 2단계로 무너진다. 추정은 근거
    문장과 함께 보여주는 것으로 충분하다.
    """
    if not card:
        return []
    fields = card.get("fields") or {}
    questions = card.get("confirm_questions") or []
    out = []
    for name in BLOCKING:
        f = fields.get(name)
        if not f or f.get("status") != "확인 필요":
            continue
        if name in OPTIONAL_UNLESS_SPOKEN and not f.get("value") and not f.get("spoken"):
            continue
        item = {
            "field": name,
            "label": f.get("label") or name,
            "value": f.get("value"),
            "spoken": f.get("spoken"),
            "evidence": f.get("evidence") or [],
            "question": _question_for(name, questions),
        }
        if name == "target":
            # 통화에서 받아 적은 성함·읍면동. 확인 전화를 걸 때 이게 있으면
            # "성함이 어떻게 되세요"가 아니라 "김말자 님 맞으실까요"로 물을 수 있다.
            item["heard"] = [
                {"label": fields[k].get("label") or k, "value": fields[k].get("value")}
                for k in HEARD if fields.get(k, {}).get("value")
            ]
        out.append(item)
    return out


# 항목 → 그 항목을 되묻는 질문에 들어 있을 법한 말. 질문 문장을 만들어 내는
# 것은 pipeline 의 일이고, 여기서는 이미 만들어진 질문 중에 고르기만 한다.
# 못 고르면 None 이다 — 없는 질문을 지어내면 통화에서 엉뚱한 걸 묻게 된다.
_QUESTION_HINTS = {
    "request": ("어떤 도움", "직접 확인", "새로운 유형"),
    # 진료과는 **확정을 막지 않는다**(BLOCKING 에 없다). 그래도 질문은 골라 줘야
    # 한다 — 통화 중 되묻기(core/followup.py)가 게이트가 막는 것보다 넓게 묻기
    # 때문이다. 막는 기준과 묻는 기준은 다르다.
    "dept": ("어느 과", "진료과"),
    # '병원' 하나로는 못 잡는다. 되묻는 문장에 들어가는 것은 **상호**이고
    # ("지난번 가셨던 ○○정형외과의원 맞으실까요?"), 이력의 상호는 의원·한의원·
    # 보건소로 끝나는 경우가 흔하다. 그러면 question 이 None 이 되어 **화면은
    # 되물을 말 없이 차단만 보여주고**, 통화 후속질문도 병원을 건너뛴다.
    # 실제로 "피부과 가야 한다"는 접수에서 그 상태를 재현했다.
    "hospital": ("병원", "의원", "가셨던", "클리닉", "보건소", "보건지소", "의료원"),
    "date": ("날짜", "언제", "며칠"),
    "time": ("시각", "시간", "오전", "오후", "몇 시"),
    # '맞으실까요' 는 넣지 않는다. 병원 질문도 그 말로 끝나서
    # ("지난번 가셨던 ○○정형외과의원 맞으실까요?") 대상자가 병원 질문을 가져갔다 —
    # 통화에서 병원을 두 번 묻고 대상자는 못 묻는 상태가 됐다.
    # 대리 접수의 대상자 질문에는 '성함과 생년' 이 들어 있어 이것만으로 잡힌다.
    "target": ("성함", "대상자", "읍면동"),
}


def _question_for(name: str, questions: list[str]) -> str | None:
    for q in questions:
        if any(h in q for h in _QUESTION_HINTS.get(name, ())):
            return q
    return None


def question_for(field: str, card: dict | None) -> str | None:
    """그 항목을 되묻는 질문. 없으면 None — **없는 질문을 지어내지 않는다.**

    질문 문장을 만드는 것은 pipeline 의 일이고 여기서는 고르기만 한다. 고르는
    규칙이 두 곳에 생기면(화면과 통화) 같은 항목에 다른 질문이 붙는다.
    """
    return _question_for(field, (card or {}).get("confirm_questions") or [])


def hard_block() -> bool:
    """확인 필요가 남으면 아예 확정을 못 하게 할 것인가.

    끄면(기본) 사회복지사가 사유를 안고 넘어갈 수 있다 — 어르신이 전화를
    끊어 버려 더 물어볼 수 없는 상황이 실제로 있다. 켜면 기관 규칙으로
    강제한다. 기관마다 다르므로 코드가 아니라 설정으로 둔다.
    """
    v = (os.environ.get("INTAKE_BLOCK_ALL_UNCONFIRMED") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def check(card: dict | None, acknowledge: bool = False) -> dict:
    """확정해도 되는가. 막을 때는 왜 막는지까지 돌려준다.

    반환값의 `allowed` 만 보고 화면이 버튼을 잠그면 된다.
    """
    items = blockers(card)
    hard = hard_block()
    if not items:
        return {"allowed": True, "blockers": [], "acknowledged": False, "hard_block": hard}
    if hard:
        # 기관 규칙이 켜져 있으면 사유를 달아도 통과시키지 않는다.
        return {"allowed": False, "blockers": items, "acknowledged": False, "hard_block": True}
    return {"allowed": bool(acknowledge), "blockers": items,
            "acknowledged": bool(acknowledge), "hard_block": False}
