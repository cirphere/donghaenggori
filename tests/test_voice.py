"""전화 연동(ClawOps VoiceML) 검증 — 실제 전화 없이 웹훅을 흉내낸다.

실행:  .venv/bin/python -m tests.test_voice

확인하는 것
  · 서명 없는 요청을 거절하는가 (이 엔드포인트는 인터넷에 열려야 한다)
  · 인사 → 녹음 VoiceML 이 나오는가
  · 녹음 콜백에서 긴급이면 <Dial> 로 통화 중 전환하는가
  · 평상시에는 접수 안내 후 <Hangup/> 인가
  · 녹음이 비었을 때 접수를 만들지 않는가
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys

from fastapi.testclient import TestClient

from donghaenggori.web import voice

KEY = "test-signing-key"
STAFF = "01099998888"
BASE = "http://testserver"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sign(url: str, params: dict) -> str:
    data = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return base64.b64encode(
        hmac.new(KEY.encode(), data.encode(), hashlib.sha256).digest()).decode()


def post(client: TestClient, path: str, params: dict, *, signed: bool = True):
    url = BASE + path
    headers = {"X-Signature": sign(url, params)} if signed else {}
    return client.post(path, data=params, headers=headers)


def main() -> int:
    voice.SIGNING_KEY = KEY
    voice.STAFF_NUMBER = STAFF

    from donghaenggori.web.api import app
    client = TestClient(app)

    incoming = {"CallId": "CA1", "AccountId": "AC1", "From": "010-1234-5678",
                "To": "07012340001", "CallStatus": "in-progress", "Direction": "inbound"}

    # 1) 서명 없는 요청은 거절 — 열린 채로 두면 누구나 접수를 만들 수 있다
    r = post(client, "/api/voice/incoming", incoming, signed=False)
    check("서명 없으면 401", r.status_code == 401, f"HTTP {r.status_code}")

    # 2) 서명이 틀려도 거절
    r = client.post("/api/voice/incoming", data=incoming, headers={"X-Signature": "bogus"})
    check("서명 틀리면 401", r.status_code == 401, f"HTTP {r.status_code}")

    # 3) 인사 + 녹음
    r = post(client, "/api/voice/incoming", incoming)
    body = r.text
    ok = (r.status_code == 200 and "<Say" in body and "<Record" in body
          and "동행고리" in body and "119" in body
          and "/api/voice/recording" in body)
    check("인사 → 녹음 VoiceML", ok, body[:70].replace("\n", " "))

    # 대화형 동사가 섞이면 안 된다 — AI가 문답을 시작하는 순간 설계가 깨진다
    check("문답(Gather) 없음", "<Gather" not in body, "")

    # 4) 긴급 → 통화 중 <Dial> 전환
    rec = dict(incoming, RecordingUrl="https://example.test/rec.wav",
               RecordingDuration="12", Digits="")
    voice._transcribe_url = lambda url: "가슴이 답답하고 숨이 차"
    r = post(client, "/api/voice/recording", rec)
    body = r.text
    ok = r.status_code == 200 and "<Dial" in body and STAFF in body
    check("긴급 → 통화 중 담당자 전환", ok, body[:70].replace("\n", " "))

    # 5) 평상시 → 접수 안내 후 종료. 전환하지 않는다.
    voice._transcribe_url = lambda url: "모레 정형외과 가야겄어. 저번에 무릎 봐준 데"
    r = post(client, "/api/voice/recording", rec)
    body = r.text
    ok = (r.status_code == 200 and "<Dial" not in body
          and "<Hangup/>" in body and "접수" in body)
    check("평상시 → 안내 후 종료", ok, body[:70].replace("\n", " "))

    # 6) 녹음이 비면 접수를 만들지 않는다
    from donghaenggori.core import db
    before = db.intake_counts().get("today", 0)
    empty = dict(incoming, RecordingUrl="", RecordingDuration="0", Digits="")
    r = post(client, "/api/voice/recording", empty)
    after = db.intake_counts().get("today", 0)
    ok = r.status_code == 200 and "<Hangup/>" in r.text and after == before
    check("빈 녹음 → 접수 안 만듦", ok, f"접수 {before} → {after}")

    # 7) 키가 없으면 연동이 꺼진다 (열어두고 통과시키지 않는다)
    voice.SIGNING_KEY = ""
    r = client.post("/api/voice/incoming", data=incoming)
    check("키 미설정이면 503", r.status_code == 503, f"HTTP {r.status_code}")
    voice.SIGNING_KEY = KEY

    print("\n전화 연동(ClawOps VoiceML) 검증")
    print("=" * 74)
    passed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:34s} {detail}")
        passed += ok
    print("=" * 74)
    print(f"  {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
