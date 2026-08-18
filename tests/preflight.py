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
import os
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
        timeout: float = 60.0, headers: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
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


def _auth(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


# 토큰 없이 부르는 검사들 — **토큰이 없어도 반드시 돈다.**
#
# 나머지 검사는 전부 --token 이 없으면 WARN 으로 건너뛴다. 그래서 인증이
# 통째로 깨져도 preflight 가 초록으로 끝나던 구간이 있었다. 인증을 도입한
# 커밋에서 무인증 거절을 확인하던 검사가 사라졌고, 그 뒤로 배포 게이트가
# "대시보드가 익명에게 열려 있다" 를 잡지 못했다.
#
# 이 두 검사는 토큰이 필요 없다. 없다는 것 자체가 검사 조건이다.

# 로그인해야만 열려야 하는 곳. 하나라도 200 이면 데이터가 익명에게 나간다.
_MUST_BE_401 = (
    ("GET", "/api/dashboard"),
    ("GET", "/api/intakes?limit=1"),
    ("GET", "/api/audit?limit=1"),
    ("GET", "/api/profiles?limit=1"),        # 건강·보호자 정보가 나가는 곳
    ("GET", "/api/profiles/010-1234-5678"),
    ("GET", "/api/status"),
    ("POST", "/api/intakes"),
)


def check_auth_required() -> None:
    open_paths = []
    for method, path in _MUST_BE_401:
        body = {"phone": "010-0000-0000", "utterance": "점검"} if method == "POST" else None
        code, _ = req(path, method=method, body=body, timeout=20)
        if code != 401:
            open_paths.append(f"{path}→{code}")
    if open_paths:
        log(FAIL, "무인증 차단", "토큰 없이 열린다: " + ", ".join(open_paths))
    else:
        log(OK, "무인증 차단", f"{len(_MUST_BE_401)}개 경로가 토큰 없이는 401")


def check_guardian_privacy() -> None:
    """보호자 경로는 무인증이어야 하고, 그 응답에 저장된 기록이 섞이면 안 된다.

    phone 을 아무나 적을 수 있어서, 프로필이 한 줄이라도 실리면 번호를 바꿔가며
    부르는 조회 API 가 된다. 실제로 그런 적이 있어서 배포 전에 매번 확인한다.
    """
    code, body = req("/api/guardian/intakes", method="POST",
                     body={"phone": "010-1234-5678", "utterance": "다음주에 병원 가야 해요"},
                     timeout=60)
    if code != 200:
        log(FAIL, "보호자 접수", f"HTTP {code} — 무인증으로 열려 있어야 한다: {body}")
        return
    blob = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
    # 시드 프로필에 있는 값들. 응답 어디에도 나오면 안 된다.
    hits = [w for w in ("박순자", "이지현", "보행기", "낙상", "장기요양",
                        "정형외과의원", "무릎", "생활지원사") if w in blob]
    leaked_keys = [k for k in ("profile", "card", "target", "facilities")
                   if isinstance(body, dict) and k in body]
    if hits or leaked_keys:
        log(FAIL, "보호자 응답 범위",
            f"저장된 기록이 응답에 섞였다 — 값 {hits} 키 {leaked_keys}")
    else:
        log(OK, "보호자 응답 범위", "무인증 200, 프로필·이력 없음")


def check_models(token: str | None) -> None:
    if not token:
        log(WARN, "상태 조회", "--token 또는 PREFLIGHT_TOKEN 이 없어 건너뜀")
        return
    code, s = req("/api/status", timeout=30, headers=_auth(token))
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


def check_warmup(token: str | None) -> None:
    if not token:
        log(WARN, "워밍업", "--token 또는 PREFLIGHT_TOKEN 이 없어 건너뜀")
        return
    t = time.time()
    code, w = req("/api/warmup", "POST", {}, timeout=600, headers=_auth(token))
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


def check_intake(token: str | None) -> None:
    if not token:
        log(WARN, "접수 파이프라인", "--token 또는 PREFLIGHT_TOKEN 이 없어 건너뜀")
        return
    t = time.time()
    code, d = req("/api/intakes", "POST",
                  {"phone": "010-1234-5678", "utterance": "모레 정형외과 가야겄어",
                   "channel": "전화", "save": False}, timeout=120, headers=_auth(token))
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

    # 이력에서 고른 병원은 '추정' 이어야 한다. 여기가 '확인됨' 으로 뜨면 배포본이
    # 옛 정책으로 돌아간 것이고, 그 값은 전화 안내까지 흘러가 어르신이 말한 적
    # 없는 병원을 "○○병원으로 접수했습니다" 로 확정해서 들려준다.
    h, hs = card.get("hospital"), card.get("hospital_status")
    if not h:
        log(WARN, "병원 후보", "없음 — 이력 조회 실패 가능")
    elif hs == "추정":
        log(OK, "병원 후보(이력 기반)", f"{h} [{hs}]")
    else:
        log(FAIL, "병원 후보(이력 기반)",
            f"{h} [{hs}] — 이력만으로 고른 병원은 '추정' 이어야 한다")

    if el > 5:
        log(WARN, "접수 응답속도", f"{el:.1f}s — 워밍업 후 1초대여야 함")


def check_hospital_policy(token: str | None) -> None:
    """직접 말한 병원과 이력에서 고른 병원이 배포본에서 실제로 구분되는가.

    check_intake 가 이력 경로('추정')를 보므로, 여기서는 반대쪽을 본다 —
    어르신이 이번에 이름을 댔는데도 '확인됨' 이 안 나오면 확정 경로가 죽은
    것이고, 그러면 복지사가 어르신이 말한 병원조차 매번 되물어야 한다.
    """
    if not token:
        log(WARN, "병원 상태 구분", "--token 또는 PREFLIGHT_TOKEN 이 없어 건너뜀")
        return
    code, d = req("/api/intakes", "POST",
                  {"phone": "010-1234-5678",
                   "utterance": "내일 순천정형외과의원 정형외과 가려고요",
                   "channel": "전화", "save": False}, timeout=120, headers=_auth(token))
    if code != 200 or not isinstance(d, dict):
        log(FAIL, "병원 상태 구분", f"HTTP {code}")
        return
    # 긴급으로 분류되면 카드 자체가 없다. 그건 의도 분류 쪽 문제이지 병원
    # 정책 문제가 아니므로, 여기서 FAIL 로 뭉뚱그리지 않고 따로 알린다.
    if d.get("card") is None:
        log(WARN, "병원 상태 구분",
            f"카드 없음(의도={d.get('intent')}) — 병원 정책을 확인하지 못했다")
        return
    card = d["card"]
    h, hs = card.get("hospital"), card.get("hospital_status")
    if h == "순천정형외과의원" and hs == "확인됨":
        log(OK, "병원 상태 구분", f"직접 언급 → {h} [{hs}]")
    else:
        log(FAIL, "병원 상태 구분",
            f"직접 언급인데 {h} [{hs}] — 어르신이 댄 이름이 확정되지 않는다")


def check_urgent(token: str | None) -> None:
    if not token:
        log(WARN, "긴급 감지", "--token 또는 PREFLIGHT_TOKEN 이 없어 건너뜀")
        return
    code, d = req("/api/intakes", "POST",
                  {"phone": "010-1234-5678", "utterance": "가슴이 답답하고 숨이 차",
                   "channel": "전화", "save": False}, timeout=120, headers=_auth(token))
    if code != 200 or not isinstance(d, dict):
        log(FAIL, "긴급 감지", f"HTTP {code}")
        return
    if d.get("urgent") and d.get("card") is None:
        log(OK, "긴급 감지", "카드 미생성 + 안내 메시지")
    else:
        log(FAIL, "긴급 감지",
            f"urgent={d.get('urgent')} card={'있음' if d.get('card') else 'None'} "
            "— 응급 발화가 일반 접수로 처리됨")


def check_rbac(mgr_token: str | None) -> None:
    """동행매니저 계정 토큰으로 확정을 시도해 403인지 본다.

    role은 이제 본문이 아니라 로그인 신원이 정한다 — 그래서 검사하려면 실제
    동행매니저 계정의 토큰이 있어야 한다. --mgr-token/PREFLIGHT_MGR_TOKEN 이
    없으면(운영자가 아직 계정을 안 만들었을 수 있다) 실패 대신 건너뛴다.
    """
    if not mgr_token:
        log(WARN, "권한 통제", "--mgr-token 또는 PREFLIGHT_MGR_TOKEN 이 없어 건너뜀 "
                              "(동행매니저 계정으로 로그인한 토큰 필요)")
        return
    code, _ = req("/api/intakes/1/confirm", "POST",
                  {"hospital": "X", "date": "2026-08-20", "level": "동행"}, timeout=30,
                  headers=_auth(mgr_token))
    if code == 403:
        log(OK, "권한 통제", "동행매니저 확정 거부 (403)")
    elif code == 404:
        log(WARN, "권한 통제", "접수 1번이 없어 검사 불가")
    elif code == 401:
        log(FAIL, "권한 통제", "토큰이 유효하지 않음 (401) — 만료됐거나 잘못된 토큰")
    else:
        log(FAIL, "권한 통제", f"HTTP {code} — 권한 없는 역할이 확정할 수 있음")


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

    # **도커로 띄운 배포일 때만 따진다.**
    #
    # 개발용 기기에서는 uvicorn 을 직접 띄우고 .env 도 없다. 그때 "프론트·터널이
    # 없다" 며 FAIL 을 내면, 시연하지 말라는 문구가 아무 의미 없이 뜬다 —
    # 그런 실패가 쌓이면 진짜 FAIL 도 무시하게 된다.
    try:
        running = subprocess.run(
            ["docker", "compose", "ps", "--services", "--filter", "status=running"],
            capture_output=True, text=True, timeout=30)
    except Exception:
        running = None
    if running is None or "app" not in running.stdout.split():
        log(WARN, "컨테이너 구성", "도커로 띄운 배포가 아니라 건너뜀 (배포 기기에서 확인할 것)")
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


def check_reset_guard(token: str | None = None) -> None:
    """초기화가 막히는지 본다. **실제로 지우지 않는다.**

    두 겹이다 — 로그인(401)과 서버 로컬(403). 토큰이 없으면 401 에서 걸리고,
    토큰이 있으면 CF 헤더 때문에 403 에서 걸린다. 어느 쪽이든 막히면 통과다.

    토큰을 주면 **바깥 겹(로컬 제한)까지** 확인된다. 안 주면 안쪽 겹만 본다 —
    그래도 "아무나 지울 수 있다" 는 여기서 걸린다.
    """
    headers = {"Content-Type": "application/json",
               "CF-Connecting-IP": "203.0.113.9",
               "CF-Ray": "preflight-test"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(BASE + "/api/reset", data=b"{}", method="POST",
                               headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            log(FAIL, "초기화 차단", f"HTTP {resp.status} — 외부에서 데이터를 지울 수 있음!")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            log(OK, "초기화 차단", "외부 요청 403 (로컬에서만 가능)")
        elif e.code == 401:
            log(OK if not token else FAIL, "초기화 차단",
                "로그인 없이는 401" if not token
                else "토큰을 줬는데 401 — 토큰이 만료됐거나 잘못됐다")
        else:
            log(FAIL, "초기화 차단", f"HTTP {e.code} — 401/403 이 아니다")
    except Exception as e:
        log(WARN, "초기화 차단", f"검사 실패: {type(e).__name__}")


def check_summary(token: str | None) -> None:
    if not token:
        log(WARN, "사후기록 요약", "--token 또는 PREFLIGHT_TOKEN 이 없어 건너뜀")
        return
    code, d = req("/api/post-records", "POST",
                  {"intake_id": 0, "phone": "010-1234-5678",
                   "memo": "무릎 주사 맞았고 다음 진료 2주 뒤, 약국 들렀어요. 계단 힘들어하셨습니다.",
                   "dept": "정형외과", "target": "박순자 어르신"}, timeout=120, headers=_auth(token))
    if code != 200 or not isinstance(d, dict):
        log(FAIL, "사후기록 요약", f"HTTP {code}")
        return
    draft = d.get("draft") or {}
    filled = sum(1 for v in draft.values() if v)
    sched = d.get("needs_schedule_check")
    log(OK if filled >= 5 and sched else WARN, "사후기록 요약",
        f"{filled}/6 항목 · 상대날짜 분리={sched} · {d.get('source')}")


def check_stt(path: str, token: str | None) -> None:
    if not token:
        log(WARN, "음성 인식", "--token 또는 PREFLIGHT_TOKEN 이 없어 건너뜀")
        return
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
                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                        **_auth(token)})
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
    ap.add_argument("--token", default=os.environ.get("PREFLIGHT_TOKEN"),
                    help="기능 확인용 사회복지사 토큰 (또는 PREFLIGHT_TOKEN 환경변수). "
                         "대부분의 API가 이제 로그인을 요구해서 이게 없으면 관련 점검을 건너뛴다")
    ap.add_argument("--mgr-token", default=os.environ.get("PREFLIGHT_MGR_TOKEN"),
                    help="권한 통제 검사용 동행매니저 토큰 (또는 PREFLIGHT_MGR_TOKEN 환경변수)")
    a = ap.parse_args()
    BASE = a.url.rstrip("/")

    print(f"\n동행고리 AI — 배포 점검  ({BASE})")
    print("=" * 74)

    if not check_reachable():
        print("  서버에 연결할 수 없습니다. 컨테이너가 떠 있는지 확인하세요.\n")
        return 1

    # 토큰 없이도 도는 것을 먼저. 이 둘이 preflight 의 최소 보장선이다 —
    # --token 을 안 주고 돌려도 인증이 깨진 배포는 여기서 걸린다.
    check_auth_required()
    check_guardian_privacy()

    check_models(a.token)
    check_warmup(a.token)
    check_intake(a.token)
    check_hospital_policy(a.token)
    check_urgent(a.token)
    check_rbac(a.mgr_token)
    check_summary(a.token)
    check_reset_guard(a.token)
    check_compose_stack()
    if a.audio:
        check_stt(a.audio, a.token)
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
