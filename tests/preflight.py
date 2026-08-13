"""배포 점검 — 시연 전에 한 번 돌린다.

    python -m tests.preflight                          # localhost:8000
    python -m tests.preflight --url https://...        # 배포 주소
    python -m tests.preflight --audio sample.webm      # STT 까지 검사

demo_scenario 와 다르다. 저쪽은 시연 흐름을 재현하려고 데이터를 초기화하지만,
이 스크립트는 **아무것도 지우지 않는다**. 지금 떠 있는 서버가 제대로 된 상태인지만 본다.

이 프로젝트에서 겪은 사고는 전부 "에러 없이 조용히 낮은 성능으로 도는" 유형이었다.
모델 권한 때문에 TF-IDF로, 마운트 누락으로 규칙 사전까지 내려간 채로 며칠을 보냈고
앱은 그동안 200을 잘 반환했다. 그래서 '동작하는지'가 아니라 **'무엇으로 동작하는지'**
를 확인한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
results: list[tuple[str, str, str]] = []   # (상태, 항목, 설명)

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


def log(state: str, name: str, detail: str = "") -> None:
    results.append((state, name, detail))


def req(path: str, method: str = "GET", body: dict | None = None,
        timeout: float = 60.0) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:                       # 연결 실패
        return 0, f"{type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────── 개별 점검 --

def check_reachable() -> bool:
    code, body = req("/api/health", timeout=15)
    if code == 200:
        log(OK, "서버 응답", f"{BASE}")
        return True
    log(FAIL, "서버 응답", f"HTTP {code} — {body}")
    return False


def check_models() -> None:
    code, s = req("/api/status", timeout=30)
    if code != 200 or not isinstance(s, dict):
        log(FAIL, "상태 조회", f"HTTP {code}")
        return

    # 의도 분류 — 무엇으로 돌고 있는지가 핵심
    model = s.get("intent_model")
    reason = s.get("intent_model_fallback_reason")
    if model == "BERT":
        log(OK, "의도 분류", "BERT (KLUE-RoBERTa)")
    elif model == "TF-IDF":
        log(WARN, "의도 분류", f"TF-IDF로 폴백 — 정확도 99.3%가 아니라 98.6%. {reason or ''}")
    else:
        log(FAIL, "의도 분류", f"{model} — 학습 모델이 전혀 안 올라감. {reason or ''}")

    # STT 장치
    stt = s.get("stt") or {}
    dev, sz = stt.get("device"), stt.get("model")
    if not stt:
        log(WARN, "STT 설정", "status에 stt 항목 없음 — 구버전 이미지")
    elif dev == "cuda":
        log(OK, "STT", f"{sz} / GPU")
    else:
        log(WARN, "STT", f"{sz} / {dev} — GPU 미사용")

    # 공공데이터
    keys = s.get("keys") or {}
    if "설정됨" in str(keys.get("DATA_GO_KR_KEY")):
        log(OK, "공공데이터 키", "설정됨")
    else:
        log(WARN, "공공데이터 키", "없음 — 병원 보강·날씨·미세먼지 미연동")

    fac = s.get("facilities") or {}
    total = sum(v for v in fac.values() if isinstance(v, int))
    (log(OK, "복지시설 적재", f"{total}건 {fac}") if total
     else log(WARN, "복지시설 적재", "0건 — 지역자원 검색이 빈 결과"))


def check_warmup() -> None:
    t = time.time()
    code, w = req("/api/warmup", "POST", {}, timeout=600)
    if code != 200 or not isinstance(w, dict):
        log(FAIL, "워밍업", f"HTTP {code}")
        return
    warmed = w.get("warmed") or {}
    bad = {k: v for k, v in warmed.items()
           if isinstance(v, str) and v not in ("ok", "loaded", "BERT", "TF-IDF")}
    el = time.time() - t
    if bad:
        # 이름만 찍으면 왜 실패했는지 서버 로그를 다시 뒤져야 한다. 이 스크립트는
        # 시연 직전에 도는 것이라, 한 줄로 원인까지 보여야 바로 고칠 수 있다.
        detail = " · ".join(f"{k}={v}" for k, v in list(bad.items())[:2])
        log(WARN, "워밍업", f"{el:.1f}s · 실패 {len(bad)}건 — {detail}")
    else:
        log(OK, "워밍업", f"{el:.1f}s · 전부 정상")


def check_intake() -> None:
    t = time.time()
    code, d = req("/api/intakes", "POST",
                  {"phone": "010-1234-5678", "utterance": "모레 정형외과 가야겄어",
                   "channel": "전화", "save": False}, timeout=120)
    el = time.time() - t
    if code != 200 or not isinstance(d, dict):
        log(FAIL, "접수 파이프라인", f"HTTP {code} — {d}")
        return

    card = d.get("card") or {}
    ok = (d.get("intent") == "병원동행" and d.get("dept") == "정형외과"
          and (d.get("date") or {}).get("date"))
    log(OK if ok else FAIL, "접수 파이프라인",
        f"{el:.1f}s · 의도={d.get('intent')} 진료과={d.get('dept')} "
        f"날짜={(d.get('date') or {}).get('date')}")

    src = d.get("intent_source")
    log(OK if src == "학습모델" else WARN, "의도 판정 경로",
        f"{src}" + ("" if src == "학습모델" else " — 규칙으로 처리됨"))

    h, hs = card.get("hospital"), card.get("hospital_status")
    log(OK if h else WARN, "병원 후보", f"{h} [{hs}]" if h else "없음 — 이력 조회 실패 가능")

    if el > 5:
        log(WARN, "접수 응답속도", f"{el:.1f}s — 워밍업 후 1초대여야 함")


def check_urgent() -> None:
    code, d = req("/api/intakes", "POST",
                  {"phone": "010-1234-5678", "utterance": "가슴이 답답하고 숨이 차",
                   "channel": "전화", "save": False}, timeout=120)
    if code != 200 or not isinstance(d, dict):
        log(FAIL, "긴급 감지", f"HTTP {code}")
        return
    if d.get("urgent") and d.get("card") is None:
        log(OK, "긴급 감지", "카드 미생성 + 안내 메시지")
    else:
        log(FAIL, "긴급 감지",
            f"urgent={d.get('urgent')} card={'있음' if d.get('card') else 'None'} "
            "— 응급 발화가 일반 접수로 처리됨")


def check_rbac() -> None:
    code, _ = req("/api/intakes/1/confirm", "POST",
                  {"hospital": "X", "date": "2026-08-20", "level": "동행",
                   "actor": "테스트", "role": "동행매니저"}, timeout=30)
    if code == 403:
        log(OK, "권한 통제", "동행매니저 확정 거부 (403)")
    elif code == 404:
        log(WARN, "권한 통제", "접수 1번이 없어 검사 불가")
    else:
        log(FAIL, "권한 통제", f"HTTP {code} — 권한 없는 역할이 확정할 수 있음")


def check_web_auth() -> None:
    """화면이 인증 뒤에 있는지 본다. 배포 주소로만 의미가 있는 점검이다.

    localhost 는 nginx 를 거치지 않고 백엔드에 바로 붙으므로 건너뛴다 —
    거기서 200 이 나오는 것은 정상이다.
    """
    if "localhost" in BASE or "127.0.0.1" in BASE:
        log(WARN, "화면 접근 제한", "로컬 주소라 건너뜀 (배포 주소로 확인할 것)")
        return
    try:
        r = urllib.request.Request(BASE + "/api/dashboard")
        with urllib.request.urlopen(r, timeout=20) as resp:
            log(FAIL, "화면 접근 제한",
                f"HTTP {resp.status} — 인증 없이 접수 목록이 보인다. "
                ".env 의 STAFF_USER/STAFF_PASSWORD 를 확인할 것")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log(OK, "화면 접근 제한", f"인증 필요 (HTTP {e.code})")
        elif e.code == 302:
            log(OK, "화면 접근 제한", "Access 로그인으로 유도됨")
        else:
            log(WARN, "화면 접근 제한", f"HTTP {e.code}")
    except Exception as e:
        log(WARN, "화면 접근 제한", f"확인 실패: {type(e).__name__}")


def check_compose_stack() -> None:
    """띄우기로 한 컨테이너가 실제로 다 떠 있는지 본다.

    `COMPOSE_FILE` 은 compose CLI 가 **루트 .env 에서만** 읽는다. 설정을
    컨테이너별로 나눈 뒤 루트 .env 를 통째로 지우면 이 값이 사라지는데,
    그러면 `docker compose up -d` 가 기본 파일만 읽어 **app 하나만 뜬다.**
    에러도 경고도 없이 프론트와 터널이 조용히 사라진다 — 접수는 되는데
    화면과 공개 주소만 죽어 있어서, 시연 직전에 알아채기 가장 어려운 형태다.

    도커 밖(호스트)에서 돌릴 때만 의미가 있다. 컨테이너 안에서는 건너뛴다.
    """
    import shutil
    import subprocess

    if not shutil.which("docker"):
        log(WARN, "컨테이너 구성", "docker 명령이 없어 건너뜀 (서버에서 확인할 것)")
        return
    try:
        out = subprocess.run(["docker", "compose", "config", "--services"],
                             capture_output=True, text=True, timeout=30)
    except Exception as e:
        log(WARN, "컨테이너 구성", f"확인 실패: {type(e).__name__}")
        return
    if out.returncode != 0:
        log(WARN, "컨테이너 구성", "compose 설정을 읽지 못함 (저장소 밖에서 실행했는지 확인)")
        return

    services = set(out.stdout.split())
    missing = {"frontend", "cloudflared"} - services
    if not missing:
        log(OK, "컨테이너 구성", f"{len(services)}개 — {', '.join(sorted(services))}")
    else:
        log(FAIL, "컨테이너 구성",
            f"{', '.join(sorted(missing))} 가 구성에 없다. "
            "루트 .env 의 COMPOSE_FILE 을 확인할 것 "
            "(지우면 app 하나만 뜬다)")


def check_reset_guard() -> None:
    """외부(터널) 요청을 흉내내 초기화가 막히는지 본다. 실제로 지우지 않는다."""
    r = urllib.request.Request(BASE + "/api/reset", data=b"{}", method="POST",
                               headers={"Content-Type": "application/json",
                                        "CF-Connecting-IP": "203.0.113.9",
                                        "CF-Ray": "preflight-test"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            log(FAIL, "초기화 차단", f"HTTP {resp.status} — 외부에서 데이터를 지울 수 있음!")
    except urllib.error.HTTPError as e:
        log(OK if e.code == 403 else FAIL, "초기화 차단",
            "외부 요청 403" if e.code == 403 else f"HTTP {e.code}")
    except Exception as e:
        log(WARN, "초기화 차단", f"검사 실패: {type(e).__name__}")


def check_summary() -> None:
    code, d = req("/api/post-records", "POST",
                  {"intake_id": 0, "phone": "010-1234-5678",
                   "memo": "무릎 주사 맞았고 다음 진료 2주 뒤, 약국 들렀어요. 계단 힘들어하셨습니다.",
                   "dept": "정형외과", "target": "박순자 어르신"}, timeout=120)
    if code != 200 or not isinstance(d, dict):
        log(FAIL, "사후기록 요약", f"HTTP {code}")
        return
    draft = d.get("draft") or {}
    filled = sum(1 for v in draft.values() if v)
    sched = d.get("needs_schedule_check")
    log(OK if filled >= 5 and sched else WARN, "사후기록 요약",
        f"{filled}/6 항목 · 상대날짜 분리={sched} · {d.get('source')}")


def check_stt(path: str) -> None:
    import mimetypes
    import uuid
    boundary = uuid.uuid4().hex
    fname = path.split("/")[-1]
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        payload = f.read()
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n").encode() + payload + \
           f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(BASE + "/api/stt", data=body, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            d = json.loads(resp.read().decode())
    except Exception as e:
        log(FAIL, "음성 인식", f"{type(e).__name__}: {e}")
        return
    el = time.time() - t
    txt = (d.get("text") or "")[:34]
    log(OK if txt else FAIL, "음성 인식",
        f"{el:.1f}s · conf {d.get('confidence')} · {txt}")
    if d.get("needs_review"):
        log(WARN, "음성 확신도", "needs_review=true — 임계값 아래")


# ────────────────────────────────────────────────────────────── 실행 --

def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description="배포 점검 (데이터를 지우지 않는다)")
    ap.add_argument("--url", default=BASE, help="점검할 주소")
    ap.add_argument("--audio", help="STT 검사용 음성 파일")
    a = ap.parse_args()
    BASE = a.url.rstrip("/")

    print(f"\n동행고리 AI — 배포 점검  ({BASE})")
    print("=" * 74)

    if not check_reachable():
        print("  서버에 연결할 수 없습니다. 컨테이너가 떠 있는지 확인하세요.\n")
        return 1

    check_models()
    check_warmup()
    check_intake()
    check_urgent()
    check_rbac()
    check_summary()
    check_reset_guard()
    check_web_auth()
    check_compose_stack()
    if a.audio:
        check_stt(a.audio)
    else:
        log(WARN, "음성 인식", "--audio 를 주지 않아 건너뜀")

    mark = {OK: "○", WARN: "△", FAIL: "×"}
    for state, name, detail in results:
        print(f"  {mark[state]} [{state}] {name:<16} {detail}")

    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    n_warn = sum(1 for s, _, _ in results if s == WARN)
    print("=" * 74)
    print(f"  {len(results)}개 점검 · 실패 {n_fail} · 주의 {n_warn}")
    if n_fail:
        print("  실패 항목을 고치기 전에는 시연하지 마세요.")
    elif n_warn:
        print("  치명적이지 않지만 확인해두는 것이 좋습니다.")
    else:
        print("  전부 정상입니다.")
    print()
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
