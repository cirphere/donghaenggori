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

    # 대상자는 되묻지 않는다 — 통화로 받아 적은 이름은 '확인됨'이 되지 않는다
    check("대상자는 되묻지 않는다", "target" not in fu.ASKABLE, str(fu.ASKABLE))

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

    # 확인 필요가 없으면 묻지 않는다
    c4 = card_of("내일 오후 2시에 송정병원 가야 해")
    check("막힌 칸이 없으면 안 묻는다", fu.next_question(c4) is None,
          str([b["field"] for b in gate.blockers(c4)]))


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
    check("다음 주소에 상태 열쇠가 실린다", nxt and "fu=" in nxt and "intake=" in nxt, str(nxt))
    xml = post(nxt, CallId="C1", From=PHONE,
               RecordingUrl="http://x/a1.wav", RecordingDuration="2")
    check("답을 받으면 통화를 끝낸다", "<Hangup/>" in xml, xml[:160])
    row = db.list_intakes(limit=1)[0]
    check("통화로 받은 값이 접수에 남는다", row["time_value"] == "15:00", str(row["time_value"]))

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

    # ── 신규 유형에는 되묻지 않는다 ──────────────────────────────
    said["text"] = "허리가 아픈데 주변에 어떤 병원이 있는지를 모르겠어"
    xml = post("/api/voice/recording", CallId="C5", From=PHONE,
               RecordingUrl="http://x/rec5.wav", RecordingDuration="7")
    check("신규 유형은 되묻지 않고 사람에게", action_of(xml) is None, str(action_of(xml)))
    check("신규 유형 안내가 나간다", "사회복지사가 확인한 뒤" in xml, xml[:200])


def main() -> int:
    db.init_db(force=True)
    test_question()
    test_handoff()
    test_reextract()
    test_record_into_intake()
    test_call_flow()

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
