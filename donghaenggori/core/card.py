"""사회복지사용 접수카드 생성 (계획서 기능 4).

접수카드 = 대상자·원문 발화·요약·병원 후보·상태·근거·확인 질문·동행 지원 수준·보호자 연락 여부
·매니저 전달사항. AI는 후보·근거까지만, 확정은 사람.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) >= 7:
        return f"{digits[:3]}-****-{digits[-4:]}"
    return "***"


# 필드 이름 → 카드 속성. to_dict 의 fields 블록을 만들 때 쓴다.
FIELD_VALUE_ATTRS = {
    "target": "target",
    # 통화에서 성함·읍면동을 물었을 때 어르신이 말한 답. 대상자를 확정하지
    # 못한 접수(미등록 번호·본인 아님)에서만 채워진다. 언제나 '확인 필요' 다.
    "spoken_name": "spoken_name",
    "spoken_region": "spoken_region",
    "hospital": "hospital",
    "dept": "dept",
    "date": "date_value",
    "time": "time_value",
}
FIELD_LABELS = {"target": "대상자", "spoken_name": "말한 성함",
                "spoken_region": "말한 주소", "hospital": "병원",
                "dept": "진료과", "date": "방문일", "time": "방문 시각"}

# 요청 유형별로 **의미가 있는 칸만** 낸다.
#
# 분류기를 4분류로 학습해 놓고 카드는 늘 병원동행 칸을 냈다. "우리 딸한테
# 연락 좀 해줘" 를 '보호자연락' 으로 맞게 분류하고도 병원·진료과 칸이 붙었고,
# 병원은 gate.BLOCKING 항목이라 **있지도 않은 병원을 확인해야 확정이 됐다.**
# 복지사는 그 칸을 왜 채워야 하는지 알 수 없다.
#
# 여기 없는 칸은 fields 에서 빠지고, 게이트는 없는 칸을 건너뛰므로(gate.blockers)
# 차단도 함께 풀린다. 값을 지우는 게 아니라 **보여주지 않는** 것이라, 평면 키
# (card.hospital 등)는 그대로 남아 나중에 유형이 바뀌어도 잃는 정보가 없다.
INTENT_FIELDS = {
    "병원동행": ("target", "spoken_name", "spoken_region", "hospital", "dept", "date", "time"),
    # 약을 대신 타 오는 요청. 어느 약국인지는 처방전을 봐야 알고 통화에서
    # 정할 수 있는 것이 아니라, 병원 칸을 세우지 않는다.
    "약국":     ("target", "spoken_name", "spoken_region", "date", "time"),
    # 보호자에게 연락해 달라는 요청. 병원도 일정도 없다.
    "보호자연락": ("target", "spoken_name", "spoken_region"),
    "기타":     ("target", "spoken_name", "spoken_region", "date", "time"),
}


@dataclass
class Card:
    target: str                      # 대상자 표시(이름/신규)
    phone_masked: str
    raw_utterance: str
    summary: str                     # AI 요약(한 줄)
    intent: str
    hospital: str | None
    hospital_status: str             # 확인됨/추정/확인 필요
    dept: str | None
    date_label: str | None
    date_value: str | None
    reasons: list[str]               # 근거(설명가능성)
    confirm_questions: list[str]     # 사회복지사 콜백용 확인 질문
    need_level: str                  # 동행 지원 수준 후보
    need_reasons: list[str]
    guardian_contact: bool
    manager_notes: list[str]

    # ── 동행을 실제로 나가는 데 필요한 것 ──────────────────────────
    #
    # **AI 가 알아낸 값이 아니라 기관이 이미 아는 값이다.** 그래서 확신도
    # 3단계를 붙이지 않는다 — 주소는 케어 프로필에 적힌 사실이지 추정이
    # 아니다. fields 에 넣지 않고 따로 싣는 이유가 그것이다.
    #
    # 이게 없으면 카드를 확정해도 배차가 안 된다. 어디로 모시러 갈지,
    # 휠체어 차량이 필요한지, 못 만났을 때 누구에게 전화할지를 모른다.
    # 실제로 카드에 5칸만 있어서 "이걸로 동행을 나갈 수 있나" 라는 물음이
    # 나왔고, 나갈 수 없었다.
    pickup: str | None = None          # 출발지 — 어르신 거주지
    mobility: str | None = None        # 거동 상태 원문 (휠체어 차량 판단)
    guardian: dict | None = None       # 보호자 이름·관계·연락처
    caregiver: str | None = None       # 생활지원사 — 현장 인계
    flags: list[str] = field(default_factory=list)   # ⚠ 필수 확인 배지 등
    # 대리 접수 — 보호자·기관이 어르신 대신 요청한 경우
    requester: str = "본인"                          # 본인 | 대리
    proxy_relation: str | None = None                # 어머니 | 아버지 | ...
    target_candidates: list[dict] = field(default_factory=list)  # 보호자 번호 역조회 결과
    # 외출 전 체크리스트 — 기상·대기 참고 정보. 방문 가부는 AI가 결정하지 않는다.
    outing_checklist: list[str] = field(default_factory=list)
    # 이력이 없을 때만 채운다 — 거리 기준 '참고 후보'. 확정 후보가 아니다(화면 04 4-A).
    reference_candidates: list[dict] = field(default_factory=list)
    # 방문 시각 — 날짜만으로는 병원 예약을 잡을 수 없다
    time_value: str | None = None            # "14:30"
    time_label: str | None = None            # "오후 2시 반"
    # 필드별 상태·근거. 지금까지 근거(reasons)가 카드 전체에 하나뿐이라
    # 화면이 "이 근거가 어느 항목 것인지"를 알 수 없었다(파일4 와이어프레임은
    # 항목마다 근거 표시를 요구한다). 아래 두 딕셔너리가 그 대응을 만든다.
    field_status: dict[str, str] = field(default_factory=dict)        # 확인됨 | 추정 | 확인 필요
    field_evidence: dict[str, list[str]] = field(default_factory=dict)
    # 동행 지원 수준을 무엇에 근거해 냈는가 — 공식 판정인지 우리 추정인지
    need_basis: str = "정보 없음"
    need_official: bool = False
    # 통화에서 어르신이 직접 말한 성함·사는 곳. 대상자를 번호로 확정하지 못한
    # 접수에서만 채운다. **절대 '확인됨' 이 되지 않는다** — 이름은 STT 오인식이
    # 잦고, 틀린 이름을 확정으로 띄우면 복지사가 그대로 부르게 된다.
    spoken_name: str | None = None
    spoken_region: str | None = None

    def fields_view(self) -> dict[str, dict]:
        """항목별 {값·상태·근거}. 평면 키(hospital, date_value…)는 그대로 두고 덧붙인다.

        확률(%)은 넣지 않는다 — 상태 3단계와 근거 문장으로만 말한다(화면 규칙 1).
        """
        # 유형에 없는 칸은 아예 내지 않는다. 모르는 유형(분류기가 새 라벨을
        # 내는 경우)은 전부 보여준다 — 빠뜨리는 것보다 낫다.
        allowed = INTENT_FIELDS.get(self.intent)
        out = {}
        for name, attr in FIELD_VALUE_ATTRS.items():
            if allowed is not None and name not in allowed:
                continue
            value = getattr(self, attr)
            out[name] = {
                "label": FIELD_LABELS[name],
                "value": value,
                # 상태를 안 채운 항목은 값 유무로 판단한다
                "status": self.field_status.get(name) or ("확인됨" if value else "확인 필요"),
                "evidence": self.field_evidence.get(name, []),
            }
        # 날짜·시각은 어르신이 말한 표현을 함께 보여줘야 확인 전화가 쉬워진다.
        # 유형에 따라 그 칸이 없을 수 있으므로 있을 때만 붙인다.
        if "date" in out:
            out["date"]["spoken"] = self.date_label
        if "time" in out:
            out["time"]["spoken"] = self.time_label

        # 말한 성함·주소는 **어떤 경로로도 '확인됨' 이 되지 않는다.**
        # 위 기본 로직은 값이 있으면 확인됨으로 올리는데, 이 둘은 8kHz 전화
        # 음질에서 규칙으로 뽑은 값이라 그 대우를 받으면 안 된다. 틀린 이름이
        # 확정으로 뜨면 복지사가 어르신을 그대로 잘못 부른다.
        # 값이 없으면 아예 빈 항목으로 두어 화면에서 사라지게 한다.
        for k in ("spoken_name", "spoken_region"):
            if k not in out:
                continue
            if out[k]["value"]:
                out[k]["status"] = "확인 필요"
            else:
                out.pop(k)
        return out

    def to_dict(self) -> dict:
        """API 응답용 — 프론트가 그대로 렌더링할 수 있는 형태."""
        return {
            "target": self.target,
            "phone_masked": self.phone_masked,
            "raw_utterance": self.raw_utterance,
            "summary": self.summary,
            "intent": self.intent,
            "hospital": self.hospital,
            "hospital_status": self.hospital_status,   # 확인됨 | 추정 | 확인 필요
            "dept": self.dept,
            "date_label": self.date_label,
            "date_value": self.date_value,
            "time_label": self.time_label,
            "time_value": self.time_value,
            "fields": self.fields_view(),
            "reasons": self.reasons,
            "confirm_questions": self.confirm_questions,
            "need_level": self.need_level,
            "need_reasons": self.need_reasons,
            "need_basis": self.need_basis,
            "need_official": self.need_official,
            "guardian_contact": self.guardian_contact,
            # 배차 정보. 값이 없으면 키는 남기고 None 을 보낸다 —
            # 화면이 "정보 없음" 과 "아직 안 붙음" 을 구분해야 한다.
            "pickup": self.pickup,
            "mobility": self.mobility,
            "guardian": self.guardian,
            "caregiver": self.caregiver,
            "manager_notes": self.manager_notes,
            "flags": self.flags,
            "requester": self.requester,
            "proxy_relation": self.proxy_relation,
            "target_candidates": self.target_candidates,
            "outing_checklist": self.outing_checklist,
            "reference_candidates": self.reference_candidates,
        }

    def to_text(self) -> str:
        L = []
        L.append("┌─────────── 사회복지사용 접수카드 ───────────")
        if self.flags:
            L.append("│ " + "  ".join(self.flags))
        L.append(f"│ 대상자   : {self.target}  ({self.phone_masked})")
        L.append(f"│ 원문 발화: \"{self.raw_utterance}\"")
        L.append(f"│ AI 요약  : {self.summary}")
        L.append(f"│ 요청 유형: {self.intent}")
        date_str = f"{self.date_label} ({self.date_value})" if self.date_value else "미확정"
        if self.time_value:
            date_str += f" {self.time_label} ({self.time_value})"
        elif self.time_label:
            date_str += f" {self.time_label} [오전·오후 확인 필요]"
        L.append(f"│ 방문 예정: {date_str}")
        hosp = self.hospital or "—"
        L.append(f"│ 병원 후보: {hosp}  [{self.hospital_status}]   진료과: {self.dept or '—'}")
        if self.reasons:
            L.append(f"│ 근거     : {' / '.join(self.reasons)}")
        if self.confirm_questions:
            L.append("│ 확인 질문(콜백):")
            for q in self.confirm_questions:
                L.append(f"│    · {q}")
        mark = "공식 판정 기준" if self.need_official else "임시 추정"
        L.append(f"│ 동행 지원 수준(후보): {self.need_level}  [{self.need_basis} · {mark}]")
        if self.need_reasons:
            L.append(f"│    근거: {', '.join(self.need_reasons)}")
        L.append(f"│ 보호자 연락 필요: {'예' if self.guardian_contact else '아니오'}")
        if self.manager_notes:
            L.append(f"│ 매니저 전달: {' / '.join(self.manager_notes)}")
        L.append("│ ※ 병원명·일정·등급은 사회복지사가 최종 확인·확정합니다.")
        L.append("└────────────────────────────────────────────")
        return "\n".join(L)
