"""의도 분류의 경계 — 파이프라인이 '무엇으로' 분류하는지 모르게 만든다.

지금까지 pipeline 이 intent_model 을 직접 import 해서 불렀다. 잘 돌지만
분류기를 바꿀 때마다 오케스트레이션 한가운데를 뜯어야 했다. 앞으로 바뀔
가능성이 높은 자리다 — 학습 모델 교체, LLM 분류기 추가, 추론을 별도
서비스로 분리(GPU 서버) 등.

그래서 분류기는 Classifier 프로토콜 뒤로 보내고, 파이프라인은 결과만 받는다.
바꿔 끼울 일이 없는 것(병원 후보 산출·동행 필요도)까지 인터페이스로 감싸지는
않았다. 구현이 하나뿐인 데 껍데기를 씌우면 읽기만 어려워진다.

**분류기를 믿지 않는다.** classify() 결과는 쓰기 전에 validate() 를 통과해야
한다. 지금은 우리 코드가 우리 코드를 검증하는 셈이라 늘 통과하지만, 여기에
LLM이나 원격 추론 서비스가 들어오면 이 한 겹이 잘못된 의도·범위 밖 확신도가
접수카드까지 새는 것을 막는다. 계약 위반은 조용히 넘기지 않고 예외로 만든다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import nlu as nlu_mod

# 긴급 임계값(0.06)은 재현율 우선으로 잡혀 있어 낮다. 그 아래로는 못 내리지만,
# 낮은 점수까지 "긴급"이라고 단정하면 문제가 생긴다 — 학습 분포 밖의 발화
# (인사·잡담·STT 오인식)가 0.06~0.13 구간에 흩어져 전부 긴급으로 뜬다.
#   실측: "고맙습니다 수고하세요" 0.112 · "여보세요?" 0.068 · 실제 긴급 0.994
#   홀드아웃 실제 긴급의 95%가 0.97 이상이다.
# 그래서 임계값은 유지하고(재현율 0.993 그대로), 이 값 미만은 '판단 보류'로
# 구분한다. 사람에게 넘기는 안전 동작은 같고, 단정만 하지 않는다.
URGENT_CONFIDENT = 0.5

# 약국·보호자연락은 학습 모델에 실데이터가 없다. C-DS01 에 대응 카테고리가
# 없어서 train_intent 가 규칙 사전으로 만든 합성 템플릿만 넣고 학습했다.
# 그래서 템플릿 밖 표현이 오면 모델이 무너진다 — 사전이 잡는 8문장으로 재보니
# 규칙 8/8, 모델 2/8 이었고, 틀릴 때도 확신은 높았다(0.993 · 0.939 · 0.906).
# 확신도 임계값으로 거를 수 없다는 뜻이다. 이 두 의도만큼은 사전이 직접 잡은
# 결과를 유지한다. 병원동행·기타는 실데이터로 학습했으므로 모델을 따른다.
RULE_OWNED_INTENTS = ("약국", "보호자연락")


class ClassifierContractError(ValueError):
    """분류기가 계약을 어겼다 — 없는 의도, 범위 밖 확신도 등."""


@dataclass
class Classification:
    analysis: nlu_mod.Analysis
    source: str                      # 학습모델 | 규칙 | 규칙+LLM | 규칙 사전(모델과 불일치)
    confidence: float | None = None
    urgent_confident: bool = True    # 긴급을 단정할 근거가 있는가


class Classifier(Protocol):
    """발화 하나를 의도·슬롯으로 바꾼다. 프로필·이력은 보지 않는다.

    조회 결과를 넣지 않는 것은 의도적이다. 분류기가 DB를 알기 시작하면
    테스트에 DB가 따라붙고, 나중에 원격 서비스로 뺄 때 같이 끌려간다.
    """

    def classify(self, utterance: str) -> Classification: ...


class HybridClassifier:
    """규칙 NLU + 학습 모델. 지금 운영에서 쓰는 구현이다.

    슬롯(진료과·증상·날짜·시각)은 규칙이 담당한다 — 결정적이어야 하는 값이라
    모델에 맡기지 않는다. 모델은 의도와 긴급 여부만 본다.
    """

    name = "hybrid"

    def __init__(self, use_llm: bool | None = None) -> None:
        self.use_llm = use_llm

    def classify(self, utterance: str) -> Classification:
        a = nlu_mod.analyze(utterance, use_llm=self.use_llm)
        source, conf = a.source, None
        rule_urgent = a.urgent          # 사전이 직접 잡은 것은 근거가 명확하다
        rule_intent = a.intent

        pred = self._predict(utterance)
        urgent_score = None
        if pred is not None:
            # 긴급은 어느 쪽이든 하나라도 걸리면 긴급 (재현율 우선)
            a.urgent = a.urgent or pred.urgent
            source, conf = "학습모델", pred.confidence
            urgent_score = pred.urgent_score
            if a.urgent:
                a.intent = "긴급"
            elif rule_intent in RULE_OWNED_INTENTS and pred.intent != rule_intent:
                # 사전이 잡은 의도를 모델이 뒤집으려 한다 — RULE_OWNED_INTENTS 참조.
                # 무엇을 근거로 정했는지 화면에 그대로 드러낸다.
                a.intent = rule_intent
                source, conf = "규칙 사전(모델과 불일치)", None
            else:
                a.intent = pred.intent

        confident = rule_urgent or (urgent_score is not None
                                    and urgent_score >= URGENT_CONFIDENT)
        return Classification(analysis=a, source=source, confidence=conf,
                              urgent_confident=confident)

    @staticmethod
    def _predict(utterance: str):
        """학습 모델 추론. 모델이 없거나 깨져도 규칙 결과로 계속 간다."""
        try:
            from ..services import intent_model
            return intent_model.predict(utterance)
        except Exception:
            return None


class RuleOnlyClassifier:
    """규칙 사전만. 학습 모델을 뺐을 때의 동작을 확인하거나 재현할 때 쓴다."""

    name = "rule-only"

    def __init__(self, use_llm: bool | None = False) -> None:
        self.use_llm = use_llm

    def classify(self, utterance: str) -> Classification:
        a = nlu_mod.analyze(utterance, use_llm=self.use_llm)
        # 규칙 사전이 긴급을 잡았다면 그 자체로 근거가 명확하다
        return Classification(analysis=a, source=a.source, urgent_confident=a.urgent)


def validate(c: Classification) -> Classification:
    """분류 결과가 계약을 지키는지 확인한다. 어기면 예외 — 조용히 넘기지 않는다."""
    if not isinstance(c, Classification):
        raise ClassifierContractError(f"Classification 이 아님: {type(c).__name__}")
    a = c.analysis
    if not isinstance(a, nlu_mod.Analysis):
        raise ClassifierContractError(f"Analysis 가 아님: {type(a).__name__}")
    if a.intent not in nlu_mod.INTENTS:
        raise ClassifierContractError(f"정의되지 않은 의도: {a.intent!r}")
    if not isinstance(a.urgent, bool):
        raise ClassifierContractError(f"urgent 가 bool 이 아님: {a.urgent!r}")
    if a.urgent and a.intent != "긴급":
        raise ClassifierContractError("urgent=True 인데 의도가 '긴급'이 아님")
    if c.confidence is not None and not 0.0 <= c.confidence <= 1.0:
        raise ClassifierContractError(f"확신도가 0~1 밖: {c.confidence}")
    if not c.source:
        raise ClassifierContractError("판정 근거(source)가 비어 있음")
    return c


def default_classifier(use_llm: bool | None = None) -> Classifier:
    return HybridClassifier(use_llm=use_llm)
