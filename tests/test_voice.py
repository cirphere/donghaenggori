"""전화 연동(ClawOps VoiceML) 검증 — 실제 전화 없이 웹훅을 흉내낸다.

실행:  .venv/bin/python -m tests.test_voice

2단계 흐름의 분기 11가지를 전부 검사한다.
  1턴  녹음 없음 / 전사 실패 / 긴급 / 본인 / 대리 1명 / 대리 여러 명 / 미등록
  2턴  답변 없음 / 긴급 / 이름 일치 / 이름 불일치
공통  서명 거절 · 키 미설정 · 중복 접수 표시
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys

from fastapi.testclient import TestClient

from donghaenggori.core import db
from donghaenggori.web import voice

KEY = "test-signing-key"
STAFF = "01099998888"
BASE = "http://testserver"

PHONE_SELF = "010-1234-5678"     # 박순자 — 등록된 대상자
PHONE_GUARD = "010-9876-5432"    # 박순자의 딸 — 보호자 번호
PHONE_NEW = "010-0000-0000"      # 미등록

results: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sign(url: str, params: dict) -> str:
    data = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return base64.b64encode(
        hmac.new(KEY.encode(), data.encode(), hashlib.sha256).digest()).decode()


def post(client, path, params, *, signed=True):
    url = BASE + path
    headers = {"X-Signature": sign(url, params)} if signed else {}
    # nginx 뒤에 있는 상태를 흉내낸다 — 스킴은 http, 원래 스킴은 헤더로 온다
    headers["X-Forwarded-Proto"] = "https"
    return client.post(path, data=params, headers=headers)


def call_params(phone: str, **extra) -> dict:
    p = {"CallId": "CA1", "AccountId": "AC1", "From": phone, "To": "07012340001",
         "CallStatus": "in-progress", "Direction": "inbound"}
    p.update(extra)
    return p


def rec_params(phone: str, seconds: str = "12") -> dict:
    return call_params(phone, RecordingUrl="https://example.test/rec.wav",
                       RecordingDuration=seconds, Digits="")


def say_next(client, phone, transcript, seconds="12"):
    """1턴 녹음 콜백 — transcript 를 말했다고 치고 응답 XML 을 돌려준다."""
    voice._transcribe_url = lambda url: transcript
    return post(client, "/api/voice/recording", rec_params(phone, seconds)).text


def main() -> int:
    voice.SIGNING_KEY = KEY
    voice.STAFF_NUMBER = STAFF
    from donghaenggori.web.api import app
    client = TestClient(app)

    # ── 공통: 서명 ───────────────────────────────────────────
    r = post(client, "/api/voice/incoming", call_params(PHONE_SELF), signed=False)
    check("서명 없으면 401", r.status_code == 401, f"HTTP {r.status_code}")

    r = client.post("/api/voice/incoming", data=call_params(PHONE_SELF),
                    headers={"X-Signature": "bogus"})
    check("서명 틀리면 401", r.status_code == 401, f"HTTP {r.status_code}")

    # URL 만·파라미터만 서명한 것은 **거부해야 한다**. 그것들은 URL 과 본문을
    # 함께 묶지 않아서, 유효한 서명 하나를 확보하면 본문을 바꿔 재사용할 수 있다.
    p0 = call_params(PHONE_SELF)
    url0 = BASE + "/api/voice/incoming"
    only_url = base64.b64encode(
        hmac.new(KEY.encode(), url0.encode(), hashlib.sha256).digest()).decode()
    r = client.post("/api/voice/incoming", data=p0,
                    headers={"X-Signature": only_url, "X-Forwarded-Proto": "https"})
    check("URL 만 서명한 것 거부", r.status_code == 401, f"HTTP {r.status_code}")

    joined = "".join(f"{k}{v}" for k, v in sorted(p0.items()))
    only_params = base64.b64encode(
        hmac.new(KEY.encode(), joined.encode(), hashlib.sha256).digest()).decode()
    r = client.post("/api/voice/incoming", data=p0,
                    headers={"X-Signature": only_params, "X-Forwarded-Proto": "https"})
    check("파라미터만 서명한 것 거부", r.status_code == 401, f"HTTP {r.status_code}")

    # 본문을 바꾸면 같은 서명이 통하지 않아야 한다(재사용 방지)
    tampered = dict(p0, From="010-9999-9999")
    r = client.post("/api/voice/incoming", data=tampered,
                    headers={"X-Signature": sign(url0, p0), "X-Forwarded-Proto": "https"})
    check("본문 변조 시 거부", r.status_code == 401, f"HTTP {r.status_code}")

    # 보내는 쪽이 query 를 빼고 서명했거나 sha256= 접두사를 붙여도 통과해야 한다
    p2 = call_params(PHONE_SELF)
    sig_noquery = sign(BASE + "/api/voice/recording", p2)
    r = client.post("/api/voice/recording?who=self", data=p2,
                    headers={"X-Signature": sig_noquery,
                             "X-Forwarded-Proto": "https"})
    check("query 없이 서명해도 통과", r.status_code == 200, f"HTTP {r.status_code}")

    r = client.post("/api/voice/incoming", data=p2,
                    headers={"X-Signature": "sha256=" + sign(BASE + "/api/voice/incoming", p2),
                             "X-Forwarded-Proto": "https"})
    check("sha256= 접두사도 통과", r.status_code == 200, f"HTTP {r.status_code}")

    # ── 콜백 주소가 https 로 나가는가 ────────────────────────
    # nginx 는 app:8000 에 http 로 붙는다. 그대로 두면 콜백이 http 로 나가고,
    # Cloudflare 가 301 로 돌리면서 POST 본문이 날아가 2턴이 깨진다.
    voice.PUBLIC_BASE_URL = ""
    body = post(client, "/api/voice/incoming", call_params(PHONE_SELF)).text
    check("프록시 뒤에서도 콜백은 https", 'action="https://' in body,
          body.split('action="')[1].split('"')[0] if 'action="' in body else "없음")

    voice.PUBLIC_BASE_URL = "https://example.test"
    body = post(client, "/api/voice/incoming", call_params(PHONE_SELF)).text
    check("PUBLIC_BASE_URL 이 우선한다",
          "https://example.test/api/voice/" in body, "")
    voice.PUBLIC_BASE_URL = ""

    # ── 1턴 인사 ─────────────────────────────────────────────
    body = post(client, "/api/voice/incoming", call_params(PHONE_SELF)).text
    ok = ("<Say" in body and "<Record" in body and "동행고리" in body
          and "/api/voice/recording" in body)
    check("인사 → 녹음", ok, "")
    # 안내 멘트에는 119 를 넣지 않는다. 긴급은 발화로 감지해 담당자로 넘기고,
    # 전환이 실패했을 때만 119 를 안내한다(아래 '연결 실패' 검사 참조).
    check("안내 멘트에 119 없음", "119" not in body, "")
    # AI 가 자유롭게 대화하면 설계가 깨진다. <Gather> 는 정해진 번호만 받으므로
    # 대화가 아니다 — 음성을 해석해 분기하는 것이 아니라 키를 세는 것이다.
    check("음성으로 분기하지 않음",
          'numDigits="1"' in body and "input=" not in body, "DTMF 만 받는다")

    # ── 본인 확인을 통화 앞에서 묻는다 ───────────────────────
    # 예전에는 녹음 → STT → 되묻기 순이었는데, 그 대기 사이에 통화가 끊겨
    # 확인 질문이 들리지 않았다. 앞으로 옮기면 STT 대기가 통화 끝으로 밀린다.
    body = post(client, "/api/voice/incoming", call_params(PHONE_SELF)).text
    ok = ("<Gather" in body and "박순자" in body and "1번" in body and "2번" in body
          and "/api/voice/identity" in body)
    check("등록 대상자 → 1번/2번 묻기", ok, "")
    # 확인 문구는 <Gather> 안에 있어야 barge-in 이 걸린다(문서). 밖으로 빼면
    # 문장이 다 끝날 때까지 키를 못 누른다.
    inside = body.split("<Gather")[1].split("</Gather>")[0]
    check("확인 문구가 <Gather> 안에 있음", "맞으신가요" in inside, inside[:120])

    # 미등록 번호 — 이름을 모르니 확인 질문은 건너뛰고 성함·읍면동부터 받는다.
    new_body = post(client, "/api/voice/incoming",
                    {"CallId": "CNEW", "From": "010-7777-0000",
                     "To": "070", "Direction": "inbound"}).text
    check("미등록: 성함·읍면동을 묻는다",
          "성함" in new_body and "읍면동" in new_body, new_body[:150])
    check("미등록: 확인 질문 없음",
          "맞으신가요" not in new_body and "<Gather" not in new_body, new_body[:150])
    # 성함은 문의와 **다른 녹음**으로 받는다. 한 번에 받으면 접수 원문에
    # 신상 이야기가 섞여, 복지사가 문의 내용을 찾아 읽어야 한다.
    check("미등록: 성함 녹음이 따로 있다",
          "/api/voice/identity-record" in new_body and "who=new" in new_body,
          new_body[:220])
    check("미등록: 성함 녹음은 짧다",
          f'maxLength="{voice.IDENTITY_SECONDS}"' in new_body
          and voice.IDENTITY_SECONDS < voice.MAX_RECORD_SECONDS, new_body[:220])

    # 무음 종료가 없어 키가 유일한 종료 수단이다. 세 안내가 모두 같은 문장을
    # 써야 한다 — 안내대로 눌렀는데 안 끝나면 어르신이 끊어 버린다.
    from donghaenggori.web import voice as V
    check("녹음 상한 60초", f'maxLength="{V.MAX_RECORD_SECONDS}"' in body
          and V.MAX_RECORD_SECONDS == 60, f"현재 {V.MAX_RECORD_SECONDS}")
    check("아무 키나 종료", all(c in V.FINISH_ON_KEY for c in "1234567890*#"), V.FINISH_ON_KEY)
    # 녹음 직전 안내에는 반드시 종료 방법이 들어가야 한다. 인사(GREETING)는
    # 뒤에 성함 질문이 또 오므로 여기서 빠진다 — 넣으면 두 번 나온다.
    check("녹음 직전 안내가 종료 방법을 알린다",
          all(V.DONE_HINT in t for t in (V.WHO_PROMPT, V.SYMPTOM_PROMPT)),
          V.DONE_HINT)
    check("인사에는 종료 안내를 넣지 않는다", V.DONE_HINT not in V.GREETING, V.GREETING)
    check("안내가 키를 누르라고 말한다", "눌러" in V.DONE_HINT, V.DONE_HINT)

    # 성함 녹음 콜백 — 여기서 실패해도 통화를 끊지 않는다. 이름을 못 받는 것보다
    # 문의를 통째로 놓치는 쪽이 훨씬 나쁘다.
    ir = post(client, "/api/voice/identity-record?who=new",
              {"CallId": "CID1", "From": PHONE_NEW, "To": "070",
               "RecordingUrl": "", "RecordingDuration": "0"})
    check("성함 녹음이 비어도 문의는 받는다",
          ir.status_code == 200 and "<Record" in ir.text
          and "who=new" in ir.text and "<Hangup" not in ir.text, ir.text[:160])
    check("성함 녹음 뒤엔 문의를 묻는다", "어디가 편찮으신지" in ir.text, ir.text[:160])

    # 앞 단계에서 받은 성함은 접수 원문에 섞이지 않는다.
    voice._remember_identity("CID2", "이영희요 목포시 용당동 삽니다")
    check("성함 발화는 CallId 로 넘긴다", voice._take_identity("CID2") is not None)
    check("한 번 꺼내면 지운다", voice._take_identity("CID2") is None)

    # 성함 녹음은 그 자리에서 전사하지 않는다 — STT 가 도는 동안 통화가
    # 무음이 되고, 무음·안내 멘트 중에 누른 키는 버려진다(finishOnKey 는
    # 녹음이 시작된 뒤에만 듣는다). "첫 질문에선 키가 먹는데 다음 질문에선
    # 안 먹는" 증상의 원인이었다. 전사는 통화 끝(/recording)으로 미룬다.
    stt_calls: list[str] = []
    prev_transcribe = voice._transcribe_url
    voice._transcribe_url = lambda url: (stt_calls.append(url), "이영희요")[1]
    ir2 = post(client, "/api/voice/identity-record?who=new",
               {"CallId": "CID_DEFER", "From": PHONE_NEW, "To": "070",
                "RecordingUrl": "https://rec/identity.wav",
                "RecordingDuration": "3"})
    check("성함 녹음을 그 자리에서 전사하지 않는다",
          ir2.status_code == 200 and not stt_calls, str(stt_calls))
    check("보관함에는 녹음 URL 이 담긴다",
          voice._take_identity("CID_DEFER") == "https://rec/identity.wav")
    voice._transcribe_url = prev_transcribe

    # 어르신이 통화에서 마지막으로 듣는 문장이다. 조사를 붙박이로 두면
    # ~내과·~치과처럼 받침 없이 끝나는 흔한 의원 이름이 "정형외과으로" 가 된다.
    class _C:
        def __init__(s, d, h, st): s.date_label, s.hospital, s.hospital_status = d, h, st
    class _R:
        def __init__(s, c): s.card = c
    for hosp, want in (("송정병원", "송정병원으로"), ("행복정형외과", "행복정형외과로"),
                       ("서울내과", "서울내과로")):
        said = V._receipt(_R(_C("내일", hosp, "확인됨")))
        check(f"접수 안내 조사 — {hosp}", want in said, said)
    check("병원 없으면 날짜만", V._receipt(_R(_C("내일", None, None))) == "내일로 접수했습니다.",
          V._receipt(_R(_C("내일", None, None))))

    # 2번(번호 주인이 아님)을 눌렀으면 그 말을 따른다. 번호 주인의 필요도·이력을
    # 그대로 붙이면, '확인 필요' 표시를 놓친 복지사가 남의 기준으로 동행을
    # 준비하게 된다. 발화에서 직접 얻은 것(병원명)은 그대로 남는다.
    from donghaenggori.core import pipeline as P
    말 = "저는 이영희인데요, 무릎이 아파서 내일 송정병원 가야 해요"
    본인 = P.run("010-1234-5678", 말, channel="전화")
    아님 = P.run("010-1234-5678", 말, channel="전화", identity_denied=True)
    check("본인이면 프로필 그대로", 본인.card.target == "박순자"
          and 본인.card.need_level != "확인 필요", 본인.card.target)
    check("2번이면 대상자를 비운다", "미확인" in 아님.card.target
          and "박순자" in 아님.card.target, 아님.card.target)
    check("2번이면 남의 필요도를 안 붙인다", 아님.card.need_level == "확인 필요",
          str(아님.card.need_level))
    check("2번이어도 말한 병원은 남는다", 아님.card.hospital == "송정병원",
          str(아님.card.hospital))

    # .env 가 코드 기본값을 덮어써서 두 번 헤맸다. 빈 값은 '미설정'이어야 한다.
    for raw, want in (("", 60), ("  ", 60), ("45", 45), ("이상한값", 60)):
        os.environ["_T_REC"] = raw
        check(f"빈/잘못된 설정도 안 터짐 ({raw!r} → {want})",
              V._int_env("_T_REC", 60) == want, str(V._int_env("_T_REC", 60)))
    os.environ.pop("_T_REC", None)

    # 시연장에서 인사말을 갈아끼울 때 종료 안내가 빠지면 1분을 기다리게 된다.
    check("커스텀 인사말에도 종료 안내가 붙는다",
          "눌러" in V._with_done_hint("동행고리입니다. 말씀해 주세요."),
          V._with_done_hint("동행고리입니다. 말씀해 주세요."))
    check("이미 안내가 있으면 덧붙이지 않는다",
          V._with_done_hint("말씀 후 아무 번호나 누르세요.") == "말씀 후 아무 번호나 누르세요.",
          V._with_done_hint("말씀 후 아무 번호나 누르세요."))

    # 키를 못 누르면 <Gather> 는 콜백 없이 끝난다. 그때 흘러갈 곳이 있어야 한다.
    check("키 안 눌러도 흘러갈 곳이 있음",
          "<Record" in body and "who=unknown" in body,
          "Gather 뒤에 Say+Record 가 따라온다")

    # 미등록 번호는 물을 이름이 없다 — 바로 증상을 받는다
    body = post(client, "/api/voice/incoming", call_params(PHONE_NEW)).text
    check("미등록 번호 → 묻지 않고 바로 녹음",
          "<Gather" not in body and "<Record" in body, "")

    # ── 1번 / 2번 / 무입력 ───────────────────────────────────
    b1 = post(client, "/api/voice/identity", call_params(PHONE_SELF, Digits="1")).text
    check("1번 → 증상을 묻고 who=self",
          "편찮으신지" in b1 and "who=self" in b1, "")

    b2 = post(client, "/api/voice/identity", call_params(PHONE_SELF, Digits="2")).text
    check("2번 → 성함을 따로 녹음받고 who=other",
          "성함" in b2 and "읍면동" in b2 and "who=other" in b2
          and "/api/voice/identity-record" in b2, b2[:200])

    b3 = post(client, "/api/voice/identity", call_params(PHONE_SELF, Digits="")).text
    check("엉뚱한 입력 → who=unknown", "who=unknown" in b3, "")

    # ── 녹음 처리 ────────────────────────────────────────────
    def newest():
        conn = db.get_conn()
        rid = conn.execute("SELECT MAX(id) FROM intakes").fetchone()[0]
        conn.close()
        return db.get_intake(rid)

    # ① 녹음 없음
    before = len(db.list_intakes(200))
    body = post(client, "/api/voice/recording?who=self",
                call_params(PHONE_SELF, RecordingUrl="", RecordingDuration="0", Digits="")).text
    check("① 녹음 없음 → 접수 안 만듦",
          "<Hangup/>" in body and len(db.list_intakes(200)) == before, "")

    # ② 전사 실패
    def boom(url):
        raise RuntimeError("stt 실패")
    voice._transcribe_url = boom
    before = len(db.list_intakes(200))
    body = post(client, "/api/voice/recording?who=self", rec_params(PHONE_SELF)).text
    check("② 전사 실패 → 접수 안 만듦",
          "알아듣지" in body and len(db.list_intakes(200)) == before, "")

    # ③ 긴급 → 통화 중 전환. 2턴으로 가지 않는다.
    body = say_next(client, PHONE_SELF, "가슴이 답답하고 숨이 차")
    check("③ 긴급 → 통화 중 <Dial>",
          "<Dial" in body and STAFF in body and "<Record" not in body, "")

    # ④ 정상 → 접수하고 끝낸다. 되묻지 않는다(앞에서 이미 물었다).
    voice._transcribe_url = lambda url: "허리 아파서 내일 송정병원으로 10시에 가야 될 것 같아"
    body = post(client, "/api/voice/recording?who=self", rec_params(PHONE_SELF)).text
    row = newest()
    check("④ 정상 → 접수 안내 후 종료",
          "<Hangup/>" in body and "<Record" not in body and "접수했습니다" in body,
          f"#{row['id']}")
    check("④ 병원명이 발화에서 잡힘", row["hospital"] == "송정병원", f"{row['hospital']}")

    # ⑤ 누른 번호가 접수에 남는다. '확인됨' 으로 올리지 않는다 —
    #    남의 폰으로 건 사람도 1번을 누를 수 있다.
    check("⑤ 1번 → 추정 (확인됨 아님)",
          row["identity_status"] == "추정" and "1번" in (row["identity_answer"] or ""),
          f"[{row['identity_status']}] {row['identity_answer']}")

    post(client, "/api/voice/recording?who=other", rec_params(PHONE_SELF))
    check("⑤ 2번 → 확인 필요", newest()["identity_status"] == "확인 필요", "")

    post(client, "/api/voice/recording?who=unknown", rec_params(PHONE_SELF))
    r = newest()
    check("⑤ 무입력 → 확인 필요",
          r["identity_status"] == "확인 필요" and "응답 없음" in (r["identity_answer"] or ""), "")

    # ── 긴급 전환 결과 ───────────────────────────────────────
    # 담당자가 못 받은 것을 아무도 모르는 상태가 제일 위험하다.
    voice._transcribe_url = lambda url: "가슴이 답답하고 숨이 차"
    body = post(client, "/api/voice/recording", rec_params(PHONE_SELF)).text
    has_action = "<Dial" in body and "action=" in body and "/api/voice/dial-result" in body
    check("긴급 <Dial> 에 결과 콜백", has_action, "")

    uid = db.list_intakes(1)[0]["id"]
    dial = f"/api/voice/dial-result?intake={uid}"
    body = post(client, dial, call_params(PHONE_SELF, DialCallStatus="no-answer")).text
    row = db.get_intake(uid)
    check("응답없음 → 기록 + 119 안내",
          row["transfer_status"] == "응답없음" and "119" in body and "<Hangup/>" in body,
          f"{row['transfer_status']}")

    body = post(client, dial, call_params(PHONE_SELF, DialCallStatus="completed")).text
    row = db.get_intake(uid)
    check("연결됨 → 기록 + 조용히 종료",
          row["transfer_status"] == "연결됨" and "119" not in body, f"{row['transfer_status']}")

    audit = [a for a in db.list_audit(10) if a["action"] == "긴급전환"]
    check("전환 결과가 감사 로그에", len(audit) >= 2, f"{len(audit)}건")

    # ── 통화 상태 웹훅 ───────────────────────────────────────
    r = post(client, "/api/voice/status",
             call_params(PHONE_SELF, CallStatus="ringing"))
    check("상태 알림 → 204, 통화 지시 없음",
          r.status_code == 204 and not r.text.strip(), f"HTTP {r.status_code}")

    # 호전환 결과가 상태 알림으로 오면 그것도 기록한다(action 콜백의 백업)
    voice._transcribe_url = lambda url: "가슴이 답답하고 숨이 차"
    post(client, "/api/voice/recording", rec_params(PHONE_SELF))
    uid2 = db.list_intakes(1)[0]["id"]
    post(client, "/api/voice/status",
         call_params(PHONE_SELF, CallStatus="completed", DialCallStatus="busy"))
    check("상태 알림의 호전환 결과도 기록",
          db.get_intake(uid2)["transfer_status"] == "통화중",
          f"{db.get_intake(uid2)['transfer_status']}")

    r = post(client, "/api/voice/status", call_params(PHONE_SELF), signed=False)
    check("상태 알림도 서명 필요", r.status_code == 401, f"HTTP {r.status_code}")

    # ── 중복 접수 표시 ───────────────────────────────────────
    # 방금 PHONE_SELF 로 여러 건 넣었으므로 다음 접수에는 중복 표시가 붙어야 한다
    voice._transcribe_url = lambda url: "모레 정형외과 가야겄어"
    post(client, "/api/voice/recording", rec_params(PHONE_SELF))
    db.list_intakes(limit=1)
    dup = db.recent_intakes(PHONE_SELF, minutes=10)
    check("중복 재전화 → 합치지 않고 표시", len(dup) > 0, f"최근 {len(dup)}건 감지")

    # ── 키 미설정 ────────────────────────────────────────────
    voice.SIGNING_KEY = ""
    r = client.post("/api/voice/incoming", data=call_params(PHONE_SELF))
    check("키 미설정이면 503", r.status_code == 503, f"HTTP {r.status_code}")
    voice.SIGNING_KEY = KEY

    # ── 안내 음성 ────────────────────────────────────────────
    # 유료 음성이라 기본은 꺼져 있어야 하고, 오타가 XML 을 깨뜨리면 안 된다.
    # <Say> 가 깨지면 통화 전체가 무음이 된다.
    check("기본은 무료 음성 — voice 속성 없음", 'voice=' not in voice._say("안녕하세요"))
    voice.VOICE = "cartesia:e1717dc3-b87b-4720-aa7f-b6db290e0609"
    check("음성 지정하면 voice 속성이 붙는다",
          'voice="cartesia:e1717dc3-b87b-4720-aa7f-b6db290e0609"' in voice._say("안녕하세요"))
    voice.VOICE = ""

    for bad in ('cartesia" onload="x', "cartesia id", "cartesia:<script>", 'a"b'):
        os.environ["CLAWOPS_VOICE"] = bad
        check(f"깨진 값은 무료로 되돌린다 — {bad!r}", voice._voice_env() == "")
    os.environ["CLAWOPS_VOICE"] = "cartesia"
    check("공급자만 지정해도 통과", voice._voice_env() == "cartesia")
    os.environ.pop("CLAWOPS_VOICE")
    check("미설정이면 빈 값", voice._voice_env() == "")

    print("\n전화 연동(ClawOps VoiceML) 2단계 흐름 검증")
    print("=" * 78)
    passed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:32s} {detail}")
        passed += ok
    print("=" * 78)
    print(f"  {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
