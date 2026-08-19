"""증상 표현 → 진료과, 사전이 놓쳤을 때의 보완 (문장 임베딩).

규칙 사전(medical_terms.json)이 1차다. 사전이 걸리면 여기까지 오지 않는다 —
결정론이 우선이고, 이 모듈은 **사전에 없는 표현만** 받는다.

어르신은 진료과 이름으로 말하지 않는다. "손발이 저려", "속이 울렁거려",
"자꾸 헛구역질이 나" 같은 표현을 전부 사전에 적어 둘 수는 없다. 적으려 할수록
한 글자 키가 늘고, 그러다 '이'(치과)가 조사와 겹쳐 "우리 딸이 데려다 준대"를
치과로 보낸 적이 있다. 사전을 무한히 키우는 대신 의미로 잇는다.

**임계값 아래는 비운다.** 유사도가 낮은데도 가장 가까운 과를 붙이면, 그게
카드에 '확인됨' 으로 올라가고 이력 필터까지 바꾼다. 모르면 모른다고 두는
편이 낫다 — 진료과는 게이트를 막지 않으므로 비워도 접수는 진행된다.

모델을 못 쓰면 조용히 None 을 돌려준다. RAG 와 같은 모델(ko-sroberta)을
쓰므로 새로 받을 것은 없다.
"""
from __future__ import annotations

import logging
import os
import threading

# 진료과를 '증상의 말'로 풀어 쓴다. 과 이름만으로는 임베딩이 증상 문장과
# 멀다 — "정형외과" 와 "손발이 저려" 는 표면적으로 아무 관계가 없다.
DEPT_DESCRIPTIONS: dict[str, str] = {
    "정형외과": "무릎 허리 어깨 관절 뼈 팔다리가 아프고 저리고 쑤시고 삐끗한 통증",
    "내과": "속이 쓰리고 더부룩하고 소화가 안 되고 혈압 당뇨 감기 기침 열 어지럼 가슴 두근거림",
    "안과": "눈이 침침하고 뿌옇고 시야가 흐리고 백내장 시력 저하",
    "이비인후과": "귀가 잘 안 들리고 코가 막히고 목이 아프고 가래 어지럼",
    "치과": "이가 아프고 잇몸이 붓고 어금니 틀니 씹기 불편",
    "피부과": "피부가 가렵고 발진 두드러기 붉게 올라오고 상처가 낫지 않음",
    "재활의학과": "물리치료 재활 운동치료 마비 후 회복 보행 훈련",
    "신경과": "손발 저림 마비 떨림 두통 어지럼 기억력 저하",
}

# 이 값 아래면 비운다. 코사인 유사도 기준이고 실측으로 잡았다.
#
#   손발이 저려      0.665  → 신경과      (잡아야 함)
#   속이 울렁거려     0.701  → 내과        (잡아야 함)
#   물리치료 받아야    0.579  → 재활의학과   (잡아야 함)
#   약 좀 타야 하는디  0.453  → 내과        ← **잡으면 안 됨**
#
# 0.45 로 뒀더니 마지막 줄이 걸렸다. 증상을 말한 적 없는 문장에 진료과를
# 붙이면 그 값이 이력 필터를 바꿔 엉뚱한 병원 후보가 나온다. 0.50 으로
# 올려 그 줄만 떨어뜨렸다 — 잡아야 할 것 중 제일 낮은 0.579 와 사이가 뜬다.
THRESHOLD = float(os.environ.get("DEPT_MATCH_THRESHOLD", "0.50"))

_log = logging.getLogger("uvicorn.error")
_lock = threading.Lock()
_state: dict = {"model": None, "emb": None, "tried": False, "reason": ""}


def _load():
    """모델과 진료과 임베딩을 한 번만 만든다. 실패하면 (None, None)."""
    if _state["tried"]:
        return _state["model"], _state["emb"]
    with _lock:
        if _state["tried"]:
            return _state["model"], _state["emb"]
        _state["tried"] = True
        try:
            from ..services import rag
            model = rag._load_model()          # RAG 와 같은 모델을 공유한다
            if model is None:
                _state["reason"] = rag.load_reason() or "임베딩 모델 없음"
            else:
                _state["model"] = model
                _state["emb"] = model.encode(
                    list(DEPT_DESCRIPTIONS.values()), convert_to_numpy=True,
                    normalize_embeddings=True, show_progress_bar=False)
        except Exception as e:
            _state["reason"] = f"{type(e).__name__}: {e}"[:200]
            _log.warning("진료과 임베딩 보완 사용 불가 — %s", _state["reason"])
        return _state["model"], _state["emb"]


def available() -> bool:
    return _load()[0] is not None


def load_reason() -> str:
    _load()
    return _state["reason"]


def guess(text: str) -> tuple[str, float] | None:
    """증상 문장 → (진료과, 유사도). 확신이 없거나 모델이 없으면 None."""
    if not text or not text.strip():
        return None
    model, emb = _load()
    if model is None or emb is None:
        return None
    try:
        q = model.encode([text], convert_to_numpy=True,
                         normalize_embeddings=True, show_progress_bar=False)
        sims = emb @ q[0]                      # 정규화되어 있어 내적 = 코사인
        idx = int(sims.argmax())
        score = float(sims[idx])
    except Exception as e:
        _log.warning("진료과 임베딩 추론 실패 — %s: %s", type(e).__name__, e)
        return None
    if score < THRESHOLD:
        return None
    return list(DEPT_DESCRIPTIONS)[idx], score
