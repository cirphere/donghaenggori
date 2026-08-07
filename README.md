# 동행고리 AI

> 사회복지사를 위한 병원동행 접수·이력정리 Copilot
> 어르신의 짧고 모호한 전화 한 통("모레 정형외과 가야겄어")을 사회복지사가 바로 확인·확정할 수 있는 **접수카드**로 바꾼다.
> **AI는 후보·근거까지만, 확정은 사람이 한다.**

2026 AI+X융합 문제발굴 산학연계 해커톤 · Track 3 · 팀 대인배

---

## 빠른 시작

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # 키가 없어도 동작합니다 (규칙 기반·미연동 폴백)

python -m donghaenggori.services.seed      # 시드 20명 · 이력 60건
python -m donghaenggori.services.loader    # 공공데이터 적재
uvicorn donghaenggori.web.api:app --reload --port 8000
```

- API 문서(Swagger): http://localhost:8000/docs ← **프론트 연동은 여기 기준**
- 개발 확인용 UI: http://localhost:8000

## 처리 흐름

```
① 발화 입력(전화·앱웹·직접)  ② STT  ③ 케어 프로필 조회  ④ 과거 이력·단골 병원
⑤ 의도 분류  ⑥ 병원 후보·날짜 해석·동행 필요도  ⑦ RAG 지역 자원 보강  ⑧ 접수카드
───────────────────────── 여기까지 AI ─────────────────────────
⑨ 사회복지사 확인·확정   ⑩ 사후 메모 → 요약 → 프로필 업데이트(승인 후)
```

긴급 신호("가슴이 답답하고 숨이 차")는 **접수카드를 만들지 않고** 즉시 사람 연결로 전환한다.
AI는 응급 여부를 판단하지 않는다.

## 구조

```
donghaenggori/
  config.py            .env 로딩 — 키 없으면 폴백
  core/
    db.py              SQLite 7테이블 · RBAC · 감사 로그
    pipeline.py        ①~⑧ 오케스트레이션
    nlu.py             규칙 NLU + 대리 접수 판별
    dateparse.py       '모레'·'다음주 화요일' → 날짜 (규칙, 결정적)
    hospital.py        병원 후보 3단계 상태
    needlevel.py       동행 지원 수준 룰엔진
    card.py            접수카드 조립
  services/
    intent_model.py    의도·긴급 분류기 (직접 학습, TF-IDF + LogisticRegression)
    stt.py             faster-whisper 로컬 추론
    summarize.py       사후기록 요약 (규칙 + LLM)
    rag.py             지역 복지자원 검색
    hira.py            심평원 병원정보 (외부 API)
    loader.py          공공데이터 CSV 적재
    seed.py            시드 데이터 생성
  web/api.py           FastAPI REST
tests/                 파일3 샘플데이터 12건 회귀 검증
```

## 설계 원칙

**병원 후보는 % 확신도가 아니라 3단계 상태로 제시한다.**

| 상태 | 조건 |
|---|---|
| 🟢 확인됨 | 발화에 직접 명시 또는 최근 6개월 같은 병원 2회 이상 |
| 🟡 추정 | 이력상 합리적 1순위 |
| 🔴 확인 필요 | 후보 동률 · 발화-이력 모순 · 이력 없음(신규) |

애매하면 숫자로 우기지 않고 '확인 필요'로 사람에게 넘긴다.

- **전화번호로 대상자를 확정하지 않는다.** 보조 식별 단서일 뿐이며, 보호자 대리 전화는 후보만 제시한다.
- **AI는 케어 프로필을 자동 변경하지 않는다.** 사회복지사가 승인한 항목만 반영되고 감사 로그에 남는다.
- **키가 없어도 동작한다.** 발표 중 키·네트워크 문제로 시연이 막히지 않게 한 설계다.

## 직접 학습하는 AI

`services/intent_model.py` — 의도 4분류 + 긴급 이진분류

- 문자 n-gram(2~4) TF-IDF + LogisticRegression. 한국어 구어체·사투리는 형태소 분석기가 자주 깨지므로 문자 단위가 어미 변형("가야겄어", "쓰겄는디")에 강하다.
- 긴급 임계값은 정확도가 아니라 **목표 재현율**로 정한다. 놓치는 것이 오탐보다 위험하다.
- 아티팩트 24KB, CPU 단건 추론 1ms 미만 → 배포 서버·노트북 어디서든 동작.

```bash
python -c "from donghaenggori.services import intent_model; ..."   # 학습
```

## 테스트

```bash
python -m tests.test_file3_cases     # 제출 문서(파일3) 12건 회귀 검증
```

## 환경변수

`.env.example` 참고. 전부 없어도 동작하며, 없으면 아래처럼 폴백한다.

| 변수 | 없을 때 |
|---|---|
| `DATA_GO_KR_KEY` | 심평원·기상·대기 미연동 — 병원 후보는 과거 이력만으로 생성 |
| `ANTHROPIC_API_KEY` | 규칙 사전 + 학습 분류기로 동작 |
| `WHISPER_MODEL` / `WHISPER_DEVICE` | small / cpu (맥은 cpu만 가능) |

현재 상태 확인: `curl localhost:8000/api/status`

## 시연 데이터 고지

대상자·이력은 **전부 가상 데이터**로 실제 개인정보를 포함하지 않는다.
공공데이터 중 콜센터 상담(C-DS01)·복지관(C-DS03)은 실데이터, 심평원·기상·대기오염은 미연동 상태다.
