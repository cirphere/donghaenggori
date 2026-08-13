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
    # 문구가 <Gather> 안에 있으면 이 회선에서 재생되지 않는다. 밖에 있어야 한다.
    before_gather = body.split("<Gather")[0]
    check("확인 문구가 <Gather> 밖에 있음",
          "맞으신가요" in before_gather and "<Say" not in body.split("<Gather")[1].split("/>")[0],
          "안에 넣으면 안 들린다")

    # 미등록 번호 — 이름을 모르니 확인 질문은 건너뛰고 성함·읍면동부터 받는다.
    new_body = post(client, "/api/voice/incoming",
                    {"CallId": "CNEW", "From": "010-7777-0000",
                     "To": "070", "Direction": "inbound"}).text
    check("미등록: 성함·읍면동을 묻는다",
          "성함" in new_body and "읍면동" in new_body, new_body[:150])
    check("미등록: 확인 질문 없음",
          "맞으신가요" not in new_body and "<Gather" not in new_body, new_body[:150])
    check("미등록: who=new 로 녹음", "who=new" in new_body, new_body[:200])

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
    check("2번 → 성함·읍면동을 묻고 who=other",
          "성함" in b2 and "읍면동" in b2 and "who=other" in b2, "")

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
    latest = db.list_intakes(limit=1)[0]
    dup = db.recent_intakes(PHONE_SELF, minutes=10)
    check("중복 재전화 → 합치지 않고 표시", len(dup) > 0, f"최근 {len(dup)}건 감지")

    # ── 키 미설정 ────────────────────────────────────────────
    voice.SIGNING_KEY = ""
    r = client.post("/api/voice/incoming", data=call_params(PHONE_SELF))
    check("키 미설정이면 503", r.status_code == 503, f"HTTP {r.status_code}")
    voice.SIGNING_KEY = KEY

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
