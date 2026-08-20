"""통화 중 후속질문 검증 — 되묻되, 우리가 답을 지어내지 않는가.

실행:  PYTHONPATH=. python -m tests.test_followup

이 기능은 통화를 길게 만든다. 그래서 지킬 것이 셋이다.

  1. **카드를 바꾸는 질문만** 한다 — 게이트가 막는 칸, 한 번에 하나
  2. **사람을 찾으면 즉시 그만둔다** — 재추출보다 handoff 판정이 먼저다
  3. **답을 못 얻으면 '확인 필요'로 남긴다** — 우리가 채우지 않는다

세 층을 본다.
  도구    질문 생성 · handoff 감지 · 재추출 (통화 없이)
  기록    답변이 접수에 어떻게 남는가 (사람이 확인한 것과 구분되는가)
  통화    실제 웹훅 왕복 — 질문이 나가고, 상한에서 멈추고, 사람을 찾으면 그만두는가
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="followup-test-"), "test.db")
os.environ["DONGHAENGGORI_DB"] = _TMP_DB
os.environ["CLAWOPS_SIGNING_KEY"] = "test-key"
os.environ["CLAWOPS_STAFF_NUMBER"] = "010-9999-0000"
# 개발기의 .env.app 이 배포 주소를 들고 있으면 콜백이 그 호스트로 나가 서명이
# 어긋난다. 테스트는 환경 파일에 흔들리지 않아야 한다(test_voice 도 같은 값을
# 손으로 돌려놓는다).
os.environ["PUBLIC_BASE_URL"] = ""

from donghaenggori.core import db, gate, pipeline  # noqa: E402
from donghaenggori.core import followup as fu  # noqa: E402

PHONE = "010-1234-5678"        # 박순자 — 등록된 대상자

results: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def card_of(utterance: str) -> dict:
    return pipeline.run(PHONE, utterance, use_llm=False, with_rag=False).card.to_dict()


def card_of_phone(phone: str, utterance: str) -> dict:
    return pipeline.run(phone, utterance, use_llm=False, with_rag=False).card.to_dict()


# ------------------------------------------------------------ 도구 --

def test_question() -> None:
    # "3시" — 오전·오후를 몰라 확인 필요로 남는 대표 케이스
    c = card_of("모레 3시에 송정병원 가야 해")
    q = fu.next_question(c)
    check("확인 필요한 칸을 되묻는다", q is not None and q.field == "time", str(q))
    check("게이트가 만든 질문을 그대로 쓴다",
          q and "오전" in q.question and "오후" in q.question, q.question if q else "")
    check("질문은 한 문장이다", q and q.question.count("?") <= 1, q.question if q else "")

    # 한 번에 하나만 — 두 칸이 막혀 있어도 질문은 하나다
    c2 = card_of("병원 좀 가야 해")
    blocked = {b["field"] for b in gate.blockers(c2)}
    q2 = fu.next_question(c2)
    check("여러 칸이 막혀도 질문은 하나", q2 is not None and isinstance(q2.question, str),
          f"막힌 칸 {blocked}")
    check("이미 물은 칸은 다시 묻지 않는다",
          fu.next_question(c2, asked=tuple(fu.pending_fields(c2))) is None)

    # 대상자는 **묻기는 하되 값을 채우지 않는다.** 8kHz 전사 이름은 어떤 경로로도
    # '확인됨'이 되지 않고, '추정'을 주면 게이트가 풀린다 — 대상자는 발신번호로도
    # 확정하지 않는다는 불변조건(AGENTS.md 3)이 무너진다.
    check("대상자도 되묻는다", "target" in fu.ASK_ORDER, str(fu.ASK_ORDER))
    rt_target = fu.reextract_field("target", "", "김말자요")
    check("대상자 답변은 기록만 하고 값은 비운다",
          rt_target.value is None and rt_target.status == "확인 필요",
          f"{rt_target.value} [{rt_target.status}]")
    check("들은 이름은 근거에 남는다",
          any("김말자" in e for e in rt_target.evidence), str(rt_target.evidence))

    # **'추정' 도 묻는다.** 단골 병원이 '추정' 인데 안 물으면, 어르신이 말한 적
    # 없는 병원으로 동행을 나간다 — 이 저장소가 제일 경계하는 사고다.
    c_reg = card_of("병원 좀 가야 해요")          # 박순자 = 정형외과 이력 2회
    check("단골 병원은 추정으로 잡힌다",
          c_reg["fields"]["hospital"]["status"] == "추정",
          str(c_reg["fields"]["hospital"]["status"]))
    check("추정이어도 되묻는다", "hospital" in fu.pending_fields(c_reg),
          str(fu.pending_fields(c_reg)))
    seq = []
    while len(seq) < fu.DEFAULT_MAX_QUESTIONS:
        q = fu.next_question(c_reg, tuple(seq))
        if not q:
            break
        seq.append(q.field)
    check("없는 것을 중복 없이 다 묻는다",
          seq == ["hospital", "date", "time", "dept"], str(seq))

    # **아는 것은 묻지 않는다.** 발신번호가 프로필과 일치하면 이름은 이미 아는
    # 것이고, 다시 묻는 것은 #105 에서 뺀 그 질문이다(답이 카드를 바꾸지 않았다).
    check("대상자가 확인됨이면 묻지 않는다",
          c_reg["fields"]["target"]["status"] == "확인됨"
          and "target" not in fu.pending_fields(c_reg), str(fu.pending_fields(c_reg)))

    # 이름을 모르는 접수는 물어진다 — 그때는 대상자가 '확인 필요' 다.
    c_new = card_of_phone("010-0000-0000", "병원 좀 가야 해요")
    check("이름을 모르면 대상자를 묻는다",
          "target" in fu.pending_fields(c_new), str(fu.pending_fields(c_new)))

    # "아니에요" 는 카드를 바꿔야 한다 — #105 에서 잃었던 안전장치다.
    r_no = fu.reextract_field("target", "", "아니에요, 제가 아니에요")
    check("본인이 아니라고 하면 상태를 내린다", r_no.downgrade, str(r_no.to_dict()))
    r_yes = fu.reextract_field("target", "", "네 맞아요")
    check("맞다고 해도 값을 올리지 않는다",
          not r_yes.downgrade and r_yes.value is None and r_yes.status == "확인 필요",
          str(r_yes.to_dict()))

    # 진료과는 게이트가 막지 않지만(없어도 동행은 나간다) 통화에서는 묻는다 —
    # 동행 정보에서 빠지면 복지사가 다시 전화하게 된다.
    c_dept = card_of("모레 오후 2시에 송정병원 가야 해요")
    check("진료과는 게이트가 막지 않는다",
          "dept" not in {b["field"] for b in gate.blockers(c_dept)},
          str([b["field"] for b in gate.blockers(c_dept)]))
    check("그래도 진료과는 되묻는다", "dept" in fu.pending_fields(c_dept),
          str(fu.pending_fields(c_dept)))

    # **게이트가 자기가 만든 질문을 찾아야 한다.** 되묻는 문장에 들어가는 것은
    # 상호이고("지난번 가셨던 ○○정형외과의원 맞으실까요?"), 이력의 상호는
    # 의원·한의원·보건소로 끝나는 경우가 흔하다. 힌트가 '병원' 하나였을 때
    # question 이 None 이 되어 **화면은 되물을 말 없이 차단만 보여주고** 통화
    # 후속질문도 병원을 건너뛰었다. 실제 이력(○○정형외과의원)으로 재현된다.
    c5 = card_of("다음주에 피부과 좀 가야 하는데요")
    hosp_blocker = next((b for b in gate.blockers(c5) if b["field"] == "hospital"), None)
    check("병원 차단에는 되물을 질문이 붙는다",
          hosp_blocker is not None and hosp_blocker["question"],
          str(hosp_blocker))
    check("'의원'으로 끝나는 상호도 되묻는다",
          fu.generate_followup_question("hospital", c5) is not None,
          str(fu.pending_fields(c5)))

    # 신규 유형은 되물어서 풀리지 않는다
    c3 = card_of("허리가 아픈데 주변에 어떤 병원이 있는지를 모르겠어")
    check("신규 유형에는 후속질문을 하지 않는다", fu.next_question(c3) is None,
          str(c3.get("request_type")))

    # 기본 정보가 다 있으면 묻지 않는다 — **아는 것을 다시 묻지 않는다.**
    c4 = card_of("내일 오후 2시에 송정병원 정형외과 가야 해요")
    check("전부 채워졌으면 안 묻는다", fu.next_question(c4) is None,
          str(fu.pending_fields(c4)))
    check("아는 것은 하나도 다시 묻지 않는다", fu.pending_fields(c4) == [],
          str(fu.pending_fields(c4)))


def test_handoff() -> None:
    cases = [
        ("사람 좀 바꿔줘", True, True),
        ("담당자한테 얘기할게", True, True),
        ("됐어요 그만할래", True, False),
        ("무슨 말인지 모르겠어", True, False),
        ("오후요", False, False),
        ("송정병원이요", False, False),
    ]
    for text, needed, explicit in cases:
        h = fu.detect_handoff_signal(text)
        check(f"handoff {needed} — {text}", h.needed == needed, f"→ {h.needed} ({h.reason})")
        if needed:
            check(f"직접요청 {explicit} — {text}", h.explicit == explicit, str(h.explicit))
            check(f"근거가 남는다 — {text}", bool(h.reason), h.reason)

    # 같은 말을 되풀이한다
    st = fu.CallState()
    st.record("time", "잘 안 들려요", clear=False)
    check("같은 답 반복 → 사람에게", fu.detect_handoff_signal("잘 안 들려요", st).needed)

    # 불명확한 답이 이어진다
    st2 = fu.CallState()
    st2.record("time", "", clear=False)
    st2.record("hospital", "어", clear=False)
    h = fu.detect_handoff_signal("음", st2)
    check("무응답이 이어지면 사람에게", h.needed, h.reason)
    check("무응답 반복은 직접요청이 아니다", not h.explicit)

    check("빈 답변은 불명확으로 센다", fu.is_unclear("") and fu.is_unclear("네"))


def test_reextract() -> None:
    c = card_of("모레 3시에 송정병원 가야 해")

    # 오전·오후만 답해도 원문의 '3시'에 붙여 확정한다
    r = fu.reextract_field("time", "모레 3시에 송정병원 가야 해", "오후요", c)
    check("오후 답변 → 15:00", r.value == "15:00" and r.status == "확인됨",
          f"{r.value} [{r.status}]")
    check("무엇을 근거로 채웠는지 남는다", any("3시" in e for e in r.evidence), str(r.evidence))

    # 못 알아들으면 확인 필요 그대로 — 우리가 고르지 않는다
    r2 = fu.reextract_field("time", "모레 3시에 가야 해", "글쎄 잘 모르겄네", c)
    check("답을 못 얻으면 확인 필요 유지", r2.value is None and r2.status == "확인 필요",
          f"{r2.value} [{r2.status}]")
    check("왜 못 채웠는지 남는다", bool(r2.evidence), str(r2.evidence))

    # 날짜
    r3 = fu.reextract_field("date", "병원 가야 해", "모레요", None)
    check("날짜 재추출", r3.value is not None and r3.status == "확인됨",
          f"{r3.value} [{r3.status}]")
    r4 = fu.reextract_field("date", "병원 가야 해", "그때쯤에", None)
    check("애매한 날짜는 안 고른다", r4.value is None, str(r4.value))

    # 병원 — 이름을 직접 대면 확인됨
    r5 = fu.reextract_field("hospital", "병원 가야 해", "송정병원이요", None)
    check("병원 이름을 직접 말하면 확인됨", r5.value == "송정병원" and r5.status == "확인됨",
          f"{r5.value} [{r5.status}]")

    # 후보를 되물어 "맞다"고 하면 **추정**이다 — 자동 전사라 확정으로 올리지 않는다
    q = "지난번 가셨던 고흥정형외과 맞으실까요?"
    r6 = fu.reextract_field("hospital", "병원 가야 해", "응 맞아", None, q)
    check("후보 확인 응답은 추정", r6.value == "고흥정형외과" and r6.status == "추정",
          f"{r6.value} [{r6.status}]")
    check("확정으로 올리지 않은 이유가 남는다",
          any("확인됨으로 올리지 않음" in e for e in r6.evidence), str(r6.evidence))

    # 아니라고 하면 후보를 지운 채 확인 필요
    r7 = fu.reextract_field("hospital", "병원 가야 해", "아니요 거기 아니에요", None, q)
    check("아니라고 하면 채우지 않는다", r7.value is None and r7.status == "확인 필요",
          f"{r7.value} [{r7.status}]")

    # 후보가 없는 질문에 "네"라고 답한 것은 동의가 아니다
    r8 = fu.reextract_field("hospital", "병원 가야 해", "네",
                            None, "어느 병원으로 모실지 말씀해 주세요.")
    check("가리킬 후보가 없으면 '네'로 채우지 않는다", r8.value is None, str(r8.value))

    # **부정을 이름 추출보다 먼저 본다.** 실통화에서 이 순서가 뒤집혀 있어
    # "백병원에는 피부과가 없으니…" 의 백병원이 '확인됨' 으로 올라갔다.
    r_neg = fu.reextract_field("hospital", "피부과 가야 해요",
                               "백병원에는 피부과가 없으니 다른 병원을 추천해 주세요", None,
                               "말씀하신 피부과는 어느 병원으로 모실까요?")
    check("없다고 한 병원을 채우지 않는다",
          r_neg.value is None and r_neg.status == "확인 필요", f"{r_neg.value} [{r_neg.status}]")
    check("추천 요청임을 근거에 남긴다",
          any("추천하지 않는다" in e for e in r_neg.evidence), str(r_neg.evidence))
    check("추천 요청은 사람 연결 신호다",
          fu.detect_handoff_signal("다른 병원을 추천해 주세요").needed)

    # 다른 칸은 건드리지 않는다
    r9 = fu.reextract_field("date", "병원 가야 해", "송정병원이요", None)
    check("날짜를 물었으면 날짜만 본다", r9.field == "date" and r9.value is None,
          f"{r9.field} {r9.value}")


# ------------------------------------------------------------ 기록 --

def test_record_into_intake() -> None:
    res = pipeline.run(PHONE, "모레 3시에 송정병원 가야 해", use_llm=False, with_rag=False)
    iid = db.save_intake(res.card, PHONE, "전화")

    q = fu.next_question(res.card.to_dict())
    r = fu.reextract_field("time", res.card.raw_utterance, "오후요", res.card.to_dict())
    row = db.apply_followup(iid, q.field, q.question, "오후요",
                            value=r.value, status=r.status, evidence=r.evidence)
    card = row["card"]
    check("값이 카드에 반영된다", card["fields"]["time"]["value"] == "15:00",
          str(card["fields"]["time"]))
    check("행의 시각 컬럼도 올라간다", row["time_value"] == "15:00", str(row["time_value"]))
    check("질문과 답변이 그대로 남는다",
          card["followups"][0]["question"] == q.question
          and card["followups"][0]["answer"] == "오후요", str(card["followups"]))
    # 화면이 뱃지 색을 고르는 값이다. result 문자열을 파싱하게 두면 문구를
    # 다듬는 순간 조용히 깨진다.
    check("상태를 키로도 남긴다", card["followups"][0]["status"] == "확인됨",
          str(card["followups"][0]))

    # **사람이 확인한 것과 구분된다** — 이게 무너지면 누가 확인했는지 답할 수 없다
    check("자동 전사에는 verified_by 가 없다",
          "verified_by" not in card["fields"]["time"], str(card["fields"]["time"]))
    db.verify_card_field(iid, "hospital", "고흥정형외과", actor="김복지", role="사회복지사")
    card2 = db.get_intake(iid)["card"]
    check("사람이 확인하면 verified_by 가 붙는다",
          card2["fields"]["hospital"].get("verified_by") == "김복지",
          str(card2["fields"]["hospital"]))

    log = db.list_audit(limit=20)
    actions = [a["action"] for a in log]
    check("감사 로그에 '후속질문'이 남는다", "후속질문" in actions, str(actions[:4]))
    check("'항목확인'과 다른 이름이다", "항목확인" in actions and "후속질문" in actions,
          str(actions[:4]))
    who = [a["actor"] for a in log if a["action"] == "후속질문"]
    check("행위자가 전화 시스템이다", who and who[0] == "전화 시스템", str(who))

    # 값을 못 얻어도 묻고 답한 사실은 남는다
    before = db.get_intake(iid)["card"]["fields"]["date"]["value"]
    db.apply_followup(iid, "date", "방문 날짜를 다시 한 번 말씀해 주세요.", "글쎄",
                      evidence=["후속답변 '글쎄' 에서 날짜를 찾지 못함"])
    card3 = db.get_intake(iid)["card"]
    check("못 채운 질문도 기록된다", len(card3["followups"]) == 2, str(card3["followups"]))
    check("못 채운 항목의 상태는 확인 필요",
          db.get_intake(iid)["card"]["followups"][-1]["status"] == "확인 필요",
          str(db.get_intake(iid)["card"]["followups"][-1]))
    check("못 채웠으면 값을 건드리지 않는다",
          card3["fields"]["date"]["value"] == before, str(card3["fields"]["date"]))
    check("못 채운 사유는 근거에 남는다",
          any("찾지 못함" in e for e in card3["fields"]["date"]["evidence"]),
          str(card3["fields"]["date"]["evidence"]))

    db.stop_followup(iid, "후속질문 2회로 마침 — 남은 항목(date)은 사회복지사 콜백 필요")
    card4 = db.get_intake(iid)["card"]
    check("중단 사유가 카드에 남는다", "콜백" in (card4.get("followup_stopped") or ""),
          str(card4.get("followup_stopped")))


# ------------------------------------------------------------ 통화 --

def test_call_flow() -> None:
    """실제 웹훅 왕복. 서명·XML 까지 그대로 지난다."""
    import base64
    import hashlib
    import hmac
    import html as html_mod
    import re

    from fastapi.testclient import TestClient

    from donghaenggori.web import api
    from donghaenggori.web import voice as v

    client = TestClient(api.app)
    BASE = "http://testserver"

    def post(path: str, **form) -> str:
        # 서명 방식은 tests/test_voice.py 와 같다 — URL 과 파라미터를 함께 묶는다.
        data = BASE + path + "".join(f"{k}{v}" for k, v in sorted(form.items()))
        sig = base64.b64encode(
            hmac.new(b"test-key", data.encode(), hashlib.sha256).digest()).decode()
        r = client.post(path, data=form,
                        headers={"X-Signature": sig, "X-Forwarded-Proto": "https"})
        check(f"POST {path.split('?')[0]} 200", r.status_code == 200, f"HTTP {r.status_code}")
        return r.text

    def action_of(xml: str) -> str | None:
        """XML 이 지시한 다음 주소를 그대로 따라간다.

        경로를 테스트가 손으로 적으면 **배선이 틀려도 테스트는 통과한다** —
        실제로 상태 열쇠를 주소에 싣도록 바꿨을 때 그랬다. 통신사가 하는 대로
        우리가 내려보낸 action 을 그대로 부른다.
        """
        m = re.search(r'<Record[^>]*action="([^"]+)"', xml)
        if not m:
            return None
        url = html_mod.unescape(m.group(1))
        # 절대 주소로 나와도 경로만 떼어 부른다 — PUBLIC_BASE_URL 이 무엇이든
        # 테스트는 같은 클라이언트로 같은 경로를 두드려야 한다.
        return re.sub(r"^https?://[^/]+", "", url)

    # 녹음 내려받기·STT 를 흉내낸다 — 회선 없이 흐름만 본다.
    said = {"text": "모레 3시에 송정병원 가야 해"}
    v._read_recording = lambda form: said["text"]

    xml = post("/api/voice/recording", CallId="C1", From=PHONE,
               RecordingUrl="http://x/rec.wav", RecordingDuration="7")
    check("확인 필요가 남으면 되묻는다", "오전인가요" in xml or "오전" in xml, xml[:160])
    check("답을 녹음으로 받는다", "<Record" in xml and "voice/followup" in xml, xml[:200])
    check("되묻는 녹음은 짧다", f'maxLength="{v.FOLLOWUP_SECONDS}"' in xml, xml[:200])
    check("끝내는 방법을 알려준다", "눌러" in xml, xml[:200])

    # 답을 하면 값이 채워지고 통화가 끝난다(남은 칸이 없으므로)
    said["text"] = "오후요"
    nxt = action_of(xml)
    check("다음 주소에 상태 열쇠가 실린다", nxt and "fu=" in nxt, str(nxt))
    # **콜백 주소에 & 를 쓰지 않는다.** XML 속성에서 &amp; 로 나가는데, 통신사
    # 파서가 엔티티를 풀지 않으면 주소가 통째로 어긋나 2턴이 조용히 깨진다.
    # 기존 콜백들은 모두 파라미터가 하나였다(?who=self, ?intake=7).
    check("콜백 주소에 & 가 없다", "&" not in xml, xml[:240])
    check("한 파라미터에 접수번호·항목이 함께 실린다",
          nxt.count("fu=") == 1 and nxt.split("fu=")[1].count(".") >= 2, str(nxt))
    xml = post(nxt, CallId="C1", From=PHONE,
               RecordingUrl="http://x/a1.wav", RecordingDuration="2")
    row = db.list_intakes(limit=1)[0]
    check("통화로 받은 값이 접수에 남는다", row["time_value"] == "15:00", str(row["time_value"]))

    # 시각을 받았으면 다음 항목(진료과)으로 이어진다 — 기본 정보를 빠짐없이 받는다
    check("남은 항목을 이어서 묻는다", "어느 과" in xml, xml[:200])
    said["text"] = "정형외과요"
    xml = post(action_of(xml), CallId="C1", From=PHONE,
               RecordingUrl="http://x/a1b.wav", RecordingDuration="2")
    check("다 받으면 통화를 끝낸다", "<Hangup/>" in xml, xml[:160])
    check("아는 것(이름)은 되묻지 않는다", "맞으신가요" not in xml, xml[:200])
    row = db.get_intake(db.list_intakes(limit=1)[0]["id"])
    check("진료과도 접수에 남는다", row["dept"] == "정형외과", str(row["dept"]))

    # ── 사람을 찾으면 즉시 그만둔다 ──────────────────────────────
    said["text"] = "모레 3시에 송정병원 가야 해"
    xml = post("/api/voice/recording", CallId="C2", From=PHONE,
               RecordingUrl="http://x/rec2.wav", RecordingDuration="7")
    said["text"] = "사람 좀 바꿔줘"
    xml = post(action_of(xml), CallId="C2", From=PHONE,
               RecordingUrl="http://x/a2.wav", RecordingDuration="2")
    check("사람을 직접 찾으면 담당자로 넘긴다", "<Dial" in xml, xml[:200])
    card = db.get_intake(db.list_intakes(limit=1)[0]["id"])["card"]
    check("사람 연결 신호가 카드에 남는다",
          "사람 연결 신호" in (card.get("followup_stopped") or ""),
          str(card.get("followup_stopped")))

    # 혼란·거부는 담당자 폰을 울리지 않는다
    said["text"] = "모레 3시에 송정병원 가야 해"
    xml = post("/api/voice/recording", CallId="C3", From=PHONE,
               RecordingUrl="http://x/rec3.wav", RecordingDuration="7")
    said["text"] = "됐어요 그만할래"
    xml = post(action_of(xml), CallId="C3", From=PHONE,
               RecordingUrl="http://x/a3.wav", RecordingDuration="2")
    check("그만하겠다면 캐묻지 않고 끝낸다", "<Hangup/>" in xml and "<Dial" not in xml,
          xml[:200])

    # ── 상한을 넘기지 않는다 ────────────────────────────────────
    said["text"] = "병원 좀 가야 해"          # 병원·날짜 둘 다 확인 필요
    xml = post("/api/voice/recording", CallId="C4", From=PHONE,
               RecordingUrl="http://x/rec4.wav", RecordingDuration="7")
    asks = 0
    for i in range(1, 5):
        nxt = action_of(xml)
        if not nxt or "followup" not in nxt:
            break
        asks += 1
        said["text"] = "글쎄 잘 모르겄어" if i == 1 else "그냥"
        xml = post(nxt, CallId="C4", From=PHONE,
                   RecordingUrl=f"http://x/b{i}.wav", RecordingDuration="2")
    check("통화당 상한을 지킨다", asks <= v.FOLLOWUP_MAX, f"{asks}회 물음")
    check("상한에서 통화가 끝난다", "<Hangup/>" in xml, xml[:160])

    # ── 상태가 사라져도(서버 재시작) 답변을 잃지 않는다 ──────────
    said["text"] = "모레 3시에 송정병원 가야 해"
    xml = post("/api/voice/recording", CallId="C6", From=PHONE,
               RecordingUrl="http://x/rec6.wav", RecordingDuration="7")
    nxt = action_of(xml)
    v._FOLLOWUP.clear()                      # 재시작을 흉내낸다
    said["text"] = "오후요"
    xml = post(nxt, CallId="C6", From=PHONE,
               RecordingUrl="http://x/a6.wav", RecordingDuration="2")
    row = db.list_intakes(limit=1)[0]
    check("상태가 없어도 답을 접수에 반영한다", row["time_value"] == "15:00",
          str(row["time_value"]))

    # ── 실통화 회귀: 백병원(정형외과 이력)에 피부과를 요청했을 때 ──────
    #
    # 실제로 이렇게 깨졌다.
    #   이력 백병원 정형외과 · 이번엔 피부과 → "지난번 가셨던 백병원 맞으실까요?"
    #   → "백병원에는 피부과가 없으니 다른 병원을 추천해 주세요"
    #   → 백병원을 '확인됨' 으로 채우고 "백병원으로 접수했습니다" 를 들려줬다
    HIST = "010-5555-1234"
    conn = db.get_conn()
    db.upsert_profile(conn, HIST, {"id": "PX", "name": "김테스트", "age": 80,
                                   "region": "전남 고흥군 ○○면"})
    conn.commit()
    conn.close()
    db.add_history(HIST, "2026-07-01", "백병원", "정형외과", "무릎")

    said["text"] = "다음주에 피부과 좀 가야 해요"
    xml = post("/api/voice/recording", CallId="C7", From=HIST,
               RecordingUrl="http://x/rec7.wav", RecordingDuration="7")
    check("진료과가 없는 병원을 되묻지 않는다", "백병원" not in xml, xml[:240])
    check("어느 병원인지를 묻는다", "어느 병원" in xml, xml[:240])

    said["text"] = "백병원에는 피부과가 없으니 다른 병원을 추천해 주세요"
    nxt = action_of(xml)
    xml = post(nxt, CallId="C7", From=HIST,
               RecordingUrl="http://x/a7.wav", RecordingDuration="3")
    check("그 답을 병원 확정으로 먹지 않는다", "백병원" not in xml, xml[:240])
    check("추천 요청은 사람에게 넘긴다", "<Hangup/>" in xml and "<Dial" not in xml, xml[:240])
    iid = db.list_intakes(limit=1)[0]["id"]
    row = db.get_intake(iid)
    check("접수의 병원 칸이 비어 있다", not row["hospital"], str(row["hospital"]))
    card = row["card"]
    check("병원 상태가 확인 필요로 남는다",
          card["fields"]["hospital"]["status"] == "확인 필요",
          str(card["fields"]["hospital"]["status"]))
    check("그만둔 이유가 남는다",
          "다른 병원" in (card.get("followup_stopped") or ""),
          str(card.get("followup_stopped")))
    check("어르신 답변은 그대로 기록된다",
          any("추천" in (f.get("answer") or "") for f in card.get("followups") or []),
          str(card.get("followups")))

    # ── 신규 유형에는 되묻지 않는다 ──────────────────────────────
    said["text"] = "허리가 아픈데 주변에 어떤 병원이 있는지를 모르겠어"
    xml = post("/api/voice/recording", CallId="C5", From=PHONE,
               RecordingUrl="http://x/rec5.wav", RecordingDuration="7")
    check("신규 유형은 되묻지 않고 사람에게", action_of(xml) is None, str(action_of(xml)))
    check("신규 유형 안내가 나간다", "사회복지사가 확인한 뒤" in xml, xml[:200])


def test_non_korean_answer() -> None:
    """한글이 없는 후속답변은 어르신의 말이 아니다.

    후속답변은 15초짜리 짧은 녹음이라 어르신이 말을 안 하면 무음이 길고,
    Whisper 는 무음에서 다른 언어를 지어낸다. 실통화에서 이것이 답변
    자리에 들어왔다.

        어느 병원으로 모실지 말씀해 주세요.
        私はもう生まれます。

    복지사 화면에 '어르신 답' 으로 뜨면 그 통화에서 무슨 일이 있었는지
    잘못 읽는다. 재추출로 넘어가 이 문장에서 병원 이름을 찾는 것은 더 나쁘다.
    """
    for 답 in ("私はもう生まれます。", "Thank you for watching",
               "サブスクライブ", "...", "", "   "):
        check(f"한글 없으면 답이 아니다 — {답[:12]!r}", fu.is_unclear(답))

    for 답 in ("오후요", "네 맞아요", "3시요", "송정병원이요", "아니 딴 데로"):
        check(f"한글 답은 그대로 받는다 — {답}", not fu.is_unclear(답))

    # 재추출로도 넘어가지 않는다 — 저 문장에서 병원을 찾으면 안 된다.
    r = fu.reextract_field("hospital", "낼 병원 가야 해", "私はもう生まれます。")
    check("한글 없는 답에서 값을 뽑지 않는다", not r.resolved, str(r.to_dict()))


def test_ambiguous_question_reaches_followup() -> None:
    """모호한 날짜·시각도 되묻는다.

    **되묻기는 게이트가 만든 질문을 다듬어 쓴다.** 그래서 게이트가 질문을
    못 고르면 통화가 조용히 건너뛴다 — 화면은 '확인 필요' 를 띄우는데
    통화는 아무것도 묻지 않는다.

    모호한 날짜 질문에는 '날짜'·'언제' 가 안 들어간다. 어르신이 말한 표현이
    그대로 들어가기 때문이다.

        "말씀하신 9월 5일 / 금요일 중에 어느 쪽으로 잡을까요?"

    병원에서 같은 일이 있었다(#117) — 상호가 들어가서 '병원' 이 없었다.
    """
    from donghaenggori.core import gate, pipeline
    for 말, 필드 in (("밝은 눈 안과 가기로 했어. 9월 5일 금요일 오후 2시로 잡아났고", "date"),
                     ("모레 세시에 정형외과 가야겄어", "time")):
        c = pipeline.run(PHONE, 말, channel="전화").card.to_dict()
        blocked = {b["field"]: b.get("question") for b in gate.blockers(c)}
        check(f"{필드} 가 막힌다", 필드 in blocked, str(list(blocked)))
        check(f"{필드} 에 되물을 말이 있다", bool(blocked.get(필드)),
              f"question={blocked.get(필드)!r}")
        q = fu.next_question(c)
        check(f"{필드} 를 되묻는다", q is not None, "질문 없음")


def main() -> int:
    db.init_db(force=True)
    test_question()
    test_handoff()
    test_reextract()
    test_record_into_intake()
    test_call_flow()
    test_non_korean_answer()
    test_ambiguous_question_reaches_followup()

    print("\n통화 중 후속질문 검증")
    print("=" * 82)
    passed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:46s} {detail}")
        passed += ok
    print("=" * 82)
    print(f"  {passed}/{len(results)} 통과   (DB: {_TMP_DB})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
