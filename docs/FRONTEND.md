# 동행고리 AI — 프론트 연동 문서

이 문서는 **살아 있는 API에서 뽑아 쓴 것**이다. 스키마가 의심스러우면 손으로 적힌 이 문서보다
Swagger가 항상 맞다. 아래 주소에서 바로 눌러볼 수 있다.

프론트와 백엔드는 **별도 컨테이너·별도 주소**로 뜬다.

| | 배포 | 로컬 |
|---|---|---|
| 프론트 | `https://donghaenggori.dohyeongops.com` | `localhost:3000` |
| **백엔드(API)** | `https://api.dohyeongops.com` | `localhost:8000` |
| **Swagger (연동 기준)** | `https://api.dohyeongops.com/docs` | `localhost:8000/docs` |

| | |
|---|---|
| 동작 확인 | `GET /api/health` → `{"status": "ok"}` |
| 현재 상태 | `GET /api/status` — 모델·키 적재 상태 |

API 주소는 **하드코딩하지 말고** `window.API_BASE`를 쓴다(실행 시점에 결정됨).
프론트 프로젝트 구성은 [`../frontend/README.md`](../frontend/README.md) 참조.

**CORS는 열려 있다.** 기본값이 모든 오리진 허용이라 `localhost:3000`이든 어디든 바로 호출된다.
운영에서 좁히려면 서버 `.env`의 `CORS_ORIGINS`에 도메인을 콤마로 나열하면 된다.

---

## 화면별 엔드포인트

| 화면 | 메서드 | 경로 |
|---|---|---|
| **01 홈** | `GET` | `/api/dashboard` — 오늘 건수 + 접수 목록 |
| | `GET` | `/api/intakes?limit=50` — 접수 목록만 |
| **02 접수** | `POST` | `/api/intakes` — 텍스트 발화 → 접수카드 |
| | `POST` | `/api/stt` — 음성 → 텍스트만 |
| | `POST` | `/api/intakes/from-audio` — 음성 → 접수카드 (한 번에) |
| **03 접수카드** | `GET` | `/api/intakes/{id}` |
| | `POST` | `/api/intakes/{id}/confirm` — 사회복지사 확정 |
| **05 사후기록** | `POST` | `/api/post-records` — 음성 메모 → 기록 초안 |
| | `POST` | `/api/post-records/{id}/approve` — 프로필 반영 승인 |
| | `GET` | `/api/post-records?limit=50` |
| 지역 자원 | `GET` | `/api/facilities?region=&query=` |
| 감사 로그 | `GET` | `/api/audit?limit=100` |

---

## 1. 접수 — `POST /api/intakes`

```json
{ "phone": "010-1234-5678", "utterance": "모레 정형외과 가야겄어", "channel": "전화" }
```

`phone`·`utterance` 필수. `channel`은 `전화` / `앱·웹(보호자)` / `직접(기관)` 중 하나(기본 `전화`).

응답 최상위:

```
urgent  channel  intent  intent_source  intent_confidence  dept  symptom
date  profile  urgent_message  facilities  card  intake_id
```

**화면에 그리는 건 대부분 `card` 안에 있다:**

| 키 | 설명 |
|---|---|
| `target` / `phone_masked` | 대상자명 / 마스킹된 번호 |
| `summary` / `raw_utterance` | 요약 / 원문 발화 |
| `hospital` + **`hospital_status`** | 병원 후보와 그 **상태** (아래 규칙 참조) |
| `dept` / `date_label` / `date_value` | 진료과 / "모레" / `2026-08-11` |
| `reasons` | 그 후보를 고른 근거 (배열) — **화면에 반드시 노출** |
| `confirm_questions` | 사회복지사에게 물어볼 확인 질문 (배열) |
| `need_level` / `need_reasons` | 동행 필요도와 근거 |
| `guardian_contact` | 보호자 연락처 |
| `requester` / `proxy_relation` / `target_candidates` | 대리 전화일 때 채워짐 |
| `outing_checklist` | 외출 전 체크(날씨·미세먼지) |
| `reference_candidates` | 이력이 없을 때 거리 기준 참고 후보 |
| `flags` | 주의 플래그 |

---

## 2. 확정 — `POST /api/intakes/{id}/confirm`

```json
{ "hospital": "○○정형외과의원", "date": "2026-08-11", "level": "휠체어·부축 동행",
  "actor": "김○○ 사회복지사", "role": "사회복지사" }
```

`role`이 `사회복지사`가 아니면 **403**이 떨어진다(동행매니저는 확정 권한 없음). 화면에서도
역할에 따라 버튼을 감추되, 서버가 최종 판정한다.

---

## 3. 음성 — `POST /api/stt`

`multipart/form-data`, 필드명 **`file`**.

```json
{ "text": "모레 정형외과 가야하는데, 같이 가주실 분 있나요?",
  "confidence": -0.214, "needs_review": false,
  "language": "ko", "duration": 3.48, "model": "medium" }
```

- **`needs_review: true`면 "확인 필요"로 표시하고 사람이 고칠 수 있게 해야 한다.** 확신도가
  낮다는 뜻이고, 그대로 접수로 넘기면 안 된다.
- 브라우저 `MediaRecorder`의 webm 그대로 보내도 된다. 확장자 없어도 통과한다.
- **5분을 넘으면 `413`** — 길이 상한이 걸려 있다. 사용자에게 안내 문구를 띄워달라.
- 마이크는 HTTPS 또는 localhost에서만 열린다. 배포 주소는 HTTPS라 문제없다.

`POST /api/intakes/from-audio?phone=...&channel=...` 은 위 STT + 접수를 한 번에 한다.
응답은 접수 응답과 같고 `stt` 키가 추가된다.

---

## 4. 사후기록 — `POST /api/post-records`

```json
{ "intake_id": 1, "phone": "010-1234-5678",
  "memo": "무릎 주사 맞았고 다음 진료 2주 뒤, 약국 들렀어요. 계단 힘들어하셨습니다.",
  "dept": "정형외과", "target": "박순자 어르신" }
```

응답의 `draft`에 6개 항목(`treatment` `next_visit` `pharmacy` `cautions` `guardian_msg`
`profile_update`)이 담긴다. `needs_schedule_check: true`면 **일정 재확인 배지**를 띄워야 한다.

승인: `POST /api/post-records/{id}/approve` — `{"approved": true, "role": "사회복지사"}`

---

## UI가 반드시 지켜야 할 규칙

이 서비스의 핵심 원칙이라 화면에서 어기면 기획 의도가 깨진다.

**1. 병원 후보는 3단계 상태로 표시한다. 확률(%)을 쓰지 않는다.**

`hospital_status`는 `확인됨` / `추정` / `확인 필요` 셋 중 하나다. "87% 확신" 같은 숫자로
바꾸지 말 것 — 사회복지사가 판단할 근거를 주는 게 목적이지 AI를 믿게 만드는 게 아니다.

**2. 근거(`reasons`)를 반드시 함께 보여준다.** 후보만 띄우고 근거를 감추면 안 된다.

**3. 상대 날짜는 확정하지 않는다.** "2주 뒤"는 그대로 두고 `needs_schedule_check`로 재확인을
유도한다. 임의로 날짜를 계산해 채우면 안 된다.

**4. AI는 케어 프로필을 자동 변경하지 않는다.** `profile_update`는 **제안**일 뿐이고,
승인 버튼을 눌러야 반영된다. 자동 저장 금지.

**5. 긴급이면 접수카드를 만들지 않는다.** `urgent: true`면 `card`가 `null`이고
`urgent_message`가 온다. 카드 대신 그 안내를 크게 띄워야 한다.

**6. 확신도가 낮은 STT는 사람이 고치게 한다** (`needs_review`).

---

## 개발할 때 알아둘 것

- **첫 요청이 느리다.** 모델 적재·외부 API 콜드 스타트로 20~30초 걸릴 수 있다.
  `POST /api/warmup` 한 번 치면 이후 1초대로 떨어진다. 데모 전에 꼭 호출할 것.
- **인증이 없다.** 배포 주소를 아는 사람은 누구나 호출할 수 있다. 단 `POST /api/reset`
  (데이터 전체 초기화)은 **외부에서 403**이고 서버 로컬에서만 동작한다.
- **데이터는 전부 가상이다.** 실제 개인정보가 아니다.
- 응답 필드가 `null`인 경우가 정상적으로 많다(이력 없음, 대리 전화 아님 등). 널 가드 필요.

막히는 부분이나 필요한 필드가 있으면 말해달라. 서버에서 맞춰줄 수 있다.

---

## 부록 — 배포 구조 (백엔드 담당자용)

```
                  Cloudflare Tunnel
                         │
     ┌───────────────────┴───────────────────┐
     │                                       │
donghaenggori.dohyeongops.com        api.dohyeongops.com
     │                                       │
  frontend:80  (nginx)                   app:8000  (FastAPI)
```

터널 대시보드에 **Public Hostname 두 개**를 만든다.

| Subdomain | Domain | Path | Service |
|---|---|---|---|
| `donghaenggori` | `dohyeongops.com` | 비움 | `HTTP` → `frontend:80` |
| `api` | `dohyeongops.com` | 비움 | `HTTP` → `app:8000` |

`localhost`가 아니라 **컨테이너 서비스 이름**(`frontend`, `app`)을 쓴다 —
cloudflared가 자기 컨테이너 안에서 돌기 때문이다.

띄우는 명령:

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.frontend.yml \
               -f docker-compose.gpu.yml \
               -f docker-compose.tunnel.yml up -d --build
```

매번 치기 번거로우면 `.env`의 `COMPOSE_FILE`에 콜론으로 나열해두면 `docker compose up -d`만으로 끝난다.
