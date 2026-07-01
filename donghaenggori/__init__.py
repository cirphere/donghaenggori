"""동행고리 AI — 사회복지사를 위한 병원동행 접수·이력정리 Copilot (예선 MVP).

파이프라인: 발화(전화/텍스트) → STT → 케어 프로필 조회 → 발화 의도 분류(하이브리드 NLU)
→ 병원 후보 생성(과거 이력 기반, 3단계 상태) → 동행 필요도 → 사회복지사용 접수카드.
AI는 후보·근거만 제시하고, 최종 확정은 사람이 한다.
"""

__all__ = ["pipeline", "nlu", "profile", "hospital", "needlevel", "card", "dateparse"]
