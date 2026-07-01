"""동행고리 AI — 사회복지사용 웹 대시보드 (Streamlit).

실행:  streamlit run app.py
화면 1: 접수(전화/음성 발화 입력) → 접수카드 생성
화면 2: 사회복지사 검토(병원명·날짜 확정, 사람 확인)
"""
import os

import streamlit as st

from donghaenggori import pipeline

st.set_page_config(page_title="동행고리 AI", page_icon="🩺", layout="centered")

# ── 헤더 ──────────────────────────────────────────────────────────
st.title("🩺 동행고리 AI")
st.caption("사회복지사를 위한 병원동행 접수·이력정리 Copilot — AI는 후보·근거만, 확정은 사람")

llm_on = bool(os.environ.get("ANTHROPIC_API_KEY"))
st.info(("발화 이해: **규칙 + Claude(LLM)**" if llm_on
         else "발화 이해: **규칙 기반** (ANTHROPIC_API_KEY 설정 시 Claude로 강화)"),
        icon="🧠")

# ── 입력(접수) ────────────────────────────────────────────────────
st.subheader("① 접수 — 어르신 전화 발화")
examples = {
    "단골(확인됨)": ("010-1234-5678", "모레 정형외과 가야겄어"),
    "이력 해석": ("010-1234-5678", "저번에 무릎 봐준 데 또 가야 쓰겄어"),
    "신규(확인 필요)": ("010-7777-8888", "다음주 화요일에 병원 가야 하는디"),
    "긴급(사람 연결)": ("010-1234-5678", "가슴이 아파서 숨이 차"),
}
pick = st.selectbox("예시 시나리오", ["(직접 입력)"] + list(examples.keys()))
def_phone, def_utt = examples.get(pick, ("010-1234-5678", ""))

col1, col2 = st.columns([1, 2])
phone = col1.text_input("발신번호", value=def_phone)
utterance = col2.text_input("발화(STT 결과)", value=def_utt, placeholder="예: 모레 정형외과 가야겄어")

if st.button("접수카드 생성", type="primary"):
    if not utterance.strip():
        st.warning("발화를 입력하세요.")
    else:
        st.session_state["result"] = pipeline.run(phone, utterance)

# ── 출력(접수카드) ────────────────────────────────────────────────
res = st.session_state.get("result")
if res is not None:
    st.subheader("② 사회복지사용 접수카드")
    st.caption(f"발화 분석: {res.analysis.source}")

    if res.urgent:
        st.error(res.urgent_message, icon="🚨")
    else:
        c = res.card
        # 필수 확인 배지
        if c.flags:
            st.warning("　".join(c.flags), icon="⚠️")

        badge = {"확인됨": "🟢", "추정": "🟡", "확인 필요": "🔴"}.get(c.hospital_status, "⚪")
        m1, m2, m3 = st.columns(3)
        m1.metric("대상자", c.target.split("(")[0])
        m2.metric("병원 후보", c.hospital or "—", f"{badge} {c.hospital_status}")
        m3.metric("방문 예정", c.date_label or "미확정")

        st.markdown(f"**원문 발화** : “{c.raw_utterance}”")
        st.markdown(f"**AI 요약** : {c.summary}")
        st.markdown(f"**진료과** : {c.dept or '—'}　|　**요청 유형** : {c.intent}")
        if c.reasons:
            st.markdown("**근거(설명가능성)** : " + " / ".join(c.reasons))
        if c.confirm_questions:
            st.markdown("**확인 질문(콜백 스크립트)**")
            for q in c.confirm_questions:
                st.markdown(f"- {q}")
        st.markdown(f"**동행 지원 수준(후보)** : {c.need_level}　"
                    f"<small>({', '.join(c.need_reasons)})</small>", unsafe_allow_html=True)
        st.markdown(f"**보호자 연락 필요** : {'예' if c.guardian_contact else '아니오'}")
        if c.manager_notes:
            st.markdown("**매니저 전달** : " + " / ".join(c.manager_notes))

        # ── 사람 확인(③) ──
        st.subheader("③ 사회복지사 확인·확정 (사람의 영역)")
        with st.form("confirm"):
            f1, f2 = st.columns(2)
            f1.text_input("병원명 확정", value=c.hospital or "")
            f2.text_input("방문일 확정", value=c.date_value or "")
            f3, f4 = st.columns(2)
            f3.selectbox("동행 지원 수준 확정", ["단순 안내", "차량+동행", "휠체어·부축 동행"],
                         index=["단순 안내", "차량+동행", "휠체어·부축 동행"].index(c.need_level)
                         if c.need_level in ["단순 안내", "차량+동행", "휠체어·부축 동행"] else 0)
            f4.checkbox("보호자에게 연락", value=c.guardian_contact)
            if st.form_submit_button("확정 및 배정 (데모)"):
                st.success("접수 확정 — 동행 매니저 배정 단계로 (데모)", icon="✅")

        with st.expander("접수카드 원문(텍스트)"):
            st.code(c.to_text())

st.divider()
st.caption("⚠ 시연용 합성 데이터. AI는 의료/응급 판단을 하지 않으며 최종 확인은 사회복지사가 합니다.")
