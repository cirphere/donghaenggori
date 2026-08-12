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

    # ── 1턴 인사 ─────────────────────────────────────────────
    body = post(client, "/api/voice/incoming", call_params(PHONE_SELF)).text
    ok = ("<Say" in body and "<Record" in body and "동행고리" in body
          and "119" in body and "/api/voice/recording" in body)
    check("인사 → 녹음", ok, "")
    check("문답(Gather) 없음", "<Gather" not in body, "AI가 대화를 시작하면 설계가 깨진다")

    # ── 1턴 분기 ─────────────────────────────────────────────
    # ① 녹음 없음
    before = len(db.list_intakes(limit=200))
    body = post(client, "/api/voice/recording",
                call_params(PHONE_SELF, RecordingUrl="", RecordingDuration="0", Digits="")).text
    after = len(db.list_intakes(limit=200))
    check("① 녹음 없음 → 접수 안 만듦", "<Hangup/>" in body and after == before,
          f"접수 {before}→{after}")

    # ② 전사 실패
    def boom(url):
        raise RuntimeError("stt 실패")
    voice._transcribe_url = boom
    before = len(db.list_intakes(limit=200))
    body = post(client, "/api/voice/recording", rec_params(PHONE_SELF)).text
    after = len(db.list_intakes(limit=200))
    check("② 전사 실패 → 접수 안 만듦",
          "<Hangup/>" in body and "알아듣지" in body and after == before, f"접수 {before}→{after}")

    # ③ 긴급 → 2턴으로 가지 않고 즉시 전환
    body = say_next(client, PHONE_SELF, "가슴이 답답하고 숨이 차")
    check("③ 긴급 → 통화 중 <Dial>",
          "<Dial" in body and STAFF in body and "<Record" not in body, "")

    # ④ 본인(등록된 번호) → 이름 확인 질문
    body = say_next(client, PHONE_SELF, "모레 정형외과 가야겄어. 저번에 무릎 봐준 데")
    check("④ 등록 대상자 → 이름 확인 질문",
          "박순자" in body and "맞으실까요" in body and "<Record" in body
          and "/api/voice/confirm" in body, "")
    intake_id = body.split("intake=")[1].split('"')[0] if "intake=" in body else ""

    # ⑤ 보호자 번호, 후보 1명
    body = say_next(client, PHONE_GUARD, "어머니 모레 정형외과 모시고 가야 해요")
    check("⑤ 대리(후보 1명) → 관계+이름 확인",
          "박순자" in body and "맞으실까요" in body and "<Record" in body, "")

    # ⑥ 보호자 번호에 후보가 둘 이상 — 시드에 없으므로 만들어서 검사한다
    #    (부부가 한 보호자를 공유하는 실제로 흔한 경우)
    conn = db.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO profiles (phone,id,name,region,guardian_json) VALUES (?,?,?,?,?)",
        ("010-5555-0001", "PT1", "테스트순자", "전남 고흥군 ○○면",
         '{"name":"테스트딸","relation":"딸","phone":"010-9876-5432"}'))
    conn.commit()
    conn.close()
    try:
        body = say_next(client, PHONE_GUARD, "어머니 모레 정형외과 모시고 가야 해요")
        check("⑥ 대리(후보 2명 이상) → 성함만 요청",
              "어느 어르신" in body and "성함" in body and "<Record" in body, "")
    finally:
        conn = db.get_conn()
        conn.execute("DELETE FROM profiles WHERE phone='010-5555-0001'")
        conn.commit()
        conn.close()

    # ⑦ 미등록 번호 → 성함·읍면동
    body = say_next(client, PHONE_NEW, "내일 병원 좀 가야 해")
    check("⑦ 미등록 → 성함·읍면동 요청",
          "성함" in body and "읍면동" in body and "<Record" in body, "")

    # ── 2턴 분기 ─────────────────────────────────────────────
    conf = f"/api/voice/confirm?intake={intake_id}"

    # ⑧ 답변 없이 끊음 → 확인 필요로 남긴다
    body = post(client, conf,
                call_params(PHONE_SELF, RecordingUrl="", RecordingDuration="0", Digits="")).text
    row = db.get_intake(int(intake_id)) if intake_id else {}
    check("⑧ 답변 없음 → 확인 필요",
          "<Hangup/>" in body and (row or {}).get("identity_status") == "확인 필요",
          f"상태={(row or {}).get('identity_status')}")

    # ⑩ 이름 일치 → 추정 (확인됨이 아니다 — 확정은 사람이 한다)
    voice._transcribe_url = lambda url: "네 박순자 맞아요"
    post(client, conf, rec_params(PHONE_SELF))
    row = db.get_intake(int(intake_id))
    check("⑩ 이름 일치 → 추정 + 원문 보존",
          row["identity_status"] == "추정" and "박순자" in (row["identity_answer"] or ""),
          f"{row['identity_status']} / {row['identity_answer']}")

    # ⑪ 이름 불일치 → 확인 필요
    voice._transcribe_url = lambda url: "아니야 나는 김철수여"
    post(client, conf, rec_params(PHONE_SELF))
    row = db.get_intake(int(intake_id))
    check("⑪ 이름 불일치 → 확인 필요",
          row["identity_status"] == "확인 필요" and "김철수" in (row["identity_answer"] or ""),
          f"{row['identity_status']} / {row['identity_answer']}")

    # ⑨ 확인 답변에서 긴급 → 전환
    voice._transcribe_url = lambda url: "아이고 숨이 차고 가슴이 답답해"
    body = post(client, conf, rec_params(PHONE_SELF)).text
    check("⑨ 확인 답변에서 긴급 → <Dial>", "<Dial" in body and STAFF in body, "")

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
