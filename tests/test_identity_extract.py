"""통화에서 말한 성함·사는 곳을 뽑는지 확인한다.

통화에서 "성함과 사시는 읍면동을 말씀해 주세요" 라고 물어놓고 답을 아무 데도
담지 않았다. 물어본 이유가 화면에 없어서, 복지사가 매번 원문을 읽어야 했다.

이 파일이 지키는 두 가지:
  1. 정형 발화에서 이름·주소를 뽑는다.
  2. **못 잡는 것보다 틀리게 잡는 쪽이 나쁘다.** "허리 아파서" 를 주소로,
     "저는 지금 무릎이" 를 이름으로 잡으면 안 된다.
  3. 뽑은 값은 **어떤 경로로도 '확인됨' 이 되지 않는다.**
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from donghaenggori.core import pipeline  # noqa: E402
from donghaenggori.core.dateparse import parse_date, parse_time, spoken_datetime  # noqa: E402
from donghaenggori.core.identity import detect_name, detect_region  # noqa: E402

_fail = 0


def check(name: str, ok, detail: str = "") -> None:
    global _fail
    if not ok:
        _fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:44} {detail}")


print("=" * 78)
print("  말한 성함·주소 추출")
print("=" * 78)

# 뽑아야 하는 것 — 어르신이 실제로 말할 법한 형태
for text, name, region in [
    ("저는 이영희고요 목포시 용당동 사는데 무릎이 아파서 내일 송정병원 가야 해요",
     "이영희", "목포시 용당동"),
    ("제 이름은 김말순입니다 신안군 압해읍 살아요", "김말순", "신안군 압해읍"),
    ("박순자라고 합니다", "박순자", None),
    ("이영희예요 용당동이요", "이영희", "용당동"),
    ("저는 최금자인데요 고흥군 도양읍이요", "최금자", "고흥군 도양읍"),
    ("제 이름은 박순자", "박순자", None),
    ("저는 정순덕이라고 합니다", "정순덕", None),
    ("하의면 사는데 병원 좀", None, "하의면"),
]:
    got = (detect_name(text), detect_region(text))
    check(f"뽑기 — {text[:20]}", got == (name, region), f"{got} (기대 {(name, region)})")

# 뽑으면 안 되는 것 — 몸 이야기·부사가 이름이나 주소로 걸리면 안 된다.
# '리' 를 주소 꼬리로 두면 "허리·머리·다리" 가 전부 걸려서 아예 뺐다.
for text in ["저는 지금 무릎이 아파요", "운동 하다가 넘어졌어요",
             "허리가 아파서 내일 병원 가야 해요", "다리가 저려서요",
             "머리가 아픈데", "가슴이 답답해요", "저는 아까 넘어졌어요",
             "어머니가 병원 가셔야 해요", "소리가 잘 안 들려요"]:
    got = (detect_name(text), detect_region(text))
    check(f"안 뽑기 — {text[:20]}", got == (None, None), str(got))

print()
print("=" * 78)
print("  접수 카드에 어떻게 실리나")
print("=" * 78)

말 = "저는 이영희고요 목포시 용당동 사는데 무릎이 아파서 내일 송정병원 가야 해요"

# 번호 주인이 아니라고 응답 / 미등록 번호 — 둘 다 대상자를 번호로 확정 못 한다
for 이름, 결과 in [
    ("2번(본인 아님)", pipeline.run("010-1234-5678", 말, channel="전화",
                                identity_denied=True)),
    ("미등록 번호", pipeline.run("010-7777-0000", 말, channel="전화")),
]:
    f = 결과.card.to_dict()["fields"]
    check(f"{이름} — 말한 성함이 실린다", f.get("spoken_name", {}).get("value") == "이영희",
          str(f.get("spoken_name")))
    check(f"{이름} — 말한 주소가 실린다",
          f.get("spoken_region", {}).get("value") == "목포시 용당동",
          str(f.get("spoken_region")))
    # 8kHz 전화 음질에서 규칙으로 뽑은 값이다. 확정으로 뜨면 복지사가 어르신을
    # 그대로 잘못 부른다.
    check(f"{이름} — 절대 확인됨이 아니다",
          f["spoken_name"]["status"] == "확인 필요"
          and f["spoken_region"]["status"] == "확인 필요",
          f'{f["spoken_name"]["status"]}/{f["spoken_region"]["status"]}')

# 번호로 대상자가 확정된 접수에는 이 칸이 아예 없어야 한다 — 화면만 시끄러워진다
본인 = pipeline.run("010-1234-5678", 말, channel="전화").card.to_dict()["fields"]
check("본인 접수에는 칸이 없다",
      "spoken_name" not in 본인 and "spoken_region" not in 본인,
      str(list(본인)))

# 이름을 못 알아들었으면 빈 칸을 만들지 않는다
없음 = pipeline.run("010-7777-0000", "무릎이 아파서 병원 가야 해요",
                  channel="전화").card.to_dict()["fields"]
check("못 뽑았으면 칸을 만들지 않는다",
      "spoken_name" not in 없음 and "spoken_region" not in 없음,
      str(list(없음)))

# 전화는 성함을 **따로 물어 받는다**. 문장 전체가 답이라 "이영희요" 처럼 이름만
# 툭 말하는 형태가 흔한데, 정형 발화를 전제한 detect_name 은 그걸 못 잡는다.
print()
print("=" * 78)
print("  성함만 따로 물었을 때의 답")
print("=" * 78)

from donghaenggori.core.identity import parse_identity_answer  # noqa: E402

for text, want in [
    ("이영희요 목포시 용당동 삽니다", ("이영희", "목포시 용당동")),
    ("김말순입니다 신안군 압해읍", ("김말순", "신안군 압해읍")),
    ("박순자", ("박순자", None)),
    ("네 박순자입니다", ("박순자", None)),
    ("제 이름은 정순덕이고 하의면 살아요", ("정순덕", "하의면")),
    ("박순자요 목포시 용당동이요", ("박순자", "목포시 용당동")),
    # 이름을 못 알아들어도 주소는 건진다
    ("목포시 용당동 삽니다", (None, "목포시 용당동")),
    # 이름이 아닌 말을 이름으로 잡으면 안 된다. 차단 목록만으로는 "이름은",
    # "잠깐만" 이 차례로 빠져나가서, 성씨로 시작하는지까지 본다.
    ("몰라요", (None, None)),
    ("네 이름이요", (None, None)),
    ("어 잠깐만요", (None, None)),
    ("그러니까요", (None, None)),
    ("잘 안 들리는데요", (None, None)),
]:
    got = parse_identity_answer(text)
    check(f"전용 답변 — {text[:18]}", got == want, f"{got} (기대 {want})")

# 전화 접수는 문의 원문에 신상 이야기가 섞이지 않아야 한다.
r = pipeline.run("010-7777-0000", "무릎이 아파서 내일 송정병원 가야 해요",
                 channel="전화", identity_utterance="이영희요 목포시 용당동 삽니다")
f = r.card.to_dict()["fields"]
check("따로 받은 성함이 실린다", f.get("spoken_name", {}).get("value") == "이영희",
      str(f.get("spoken_name")))
check("원문에는 문의만 남는다",
      "이영희" not in r.card.raw_utterance and "용당동" not in r.card.raw_utterance,
      r.card.raw_utterance)

# ── 보호자 포털 구조화 신청 → 발화 합성 ──────────────────────────────
#
# spoken_datetime 이 만든 표기를 **이 파일의 파서가 다시 읽을 수 있어야** 한다.
# ISO(2026-08-20 / 14:30)를 그대로 문장에 넣던 시절엔 parse_date 도 parse_time
# 도 못 읽어서, 보호자가 달력에서 고른 날짜가 통째로 버려졌다. date 가 None 이
# 되면 방문일이 '확인 필요' 로 남고 그건 gate.BLOCKING 항목이라 확정이 409 로
# 막힌다 — 보호자가 정확히 적어 보낸 일정을 사회복지사가 다시 전화로 확인해야
# 했다. 한쪽만 고치면 조용히 돌아오는 종류라 왕복으로 잠근다.
_TODAY = datetime.date(2026, 8, 18)
for date, time, want_d, want_t in [
    ("2026-08-20", "14:30", "2026-08-20", "14:30"),
    ("2026-08-20", "09:00", "2026-08-20", "09:00"),   # 정각은 '9시' 로 줄인다
    ("2026-08-20", None,    "2026-08-20", None),      # 시간 미정
    ("2027-01-05", "10:05", "2027-01-05", "10:05"),   # 연말 경계
]:
    text = spoken_datetime(date, time)
    got_d = (parse_date(text, today=_TODAY) or {}).get("date")
    got_t = (parse_time(text) or {}).get("time")
    check(f"폼 일정 왕복 — {date} {time}", (got_d, got_t) == (want_d, want_t),
          f"'{text}' → {got_d} {got_t} (기대 {want_d} {want_t})")

# ── 주소 → 좌표 (보호자 포털의 주소 검색 대응) ──────────────────────
#
# 프로필에는 '전남 화순군 ○○면' 처럼 줄임 표기가 들어 있는데, 주소 검색은
# 법정 표기('전라남도 화순군 …')를 준다. startswith 로만 맞추던 시절엔 전남
# 지역이 통째로 좌표를 못 찾아 신규 대상자의 '거리 기준 참고 후보'가 조용히
# 사라졌다. 반대 방향 실수도 있었다 — 부분 문자열로 맞췄더니 '강남구' 안의
# '남구' 가 걸려 서울 주소가 광주 좌표를 받았다.
from donghaenggori.core import geo  # noqa: E402

_HWASUN = (35.0645, 126.9866)
for text, want in [
    ("전남 화순군 ○○면", _HWASUN),              # 기존 프로필 표기
    ("전라남도 화순군 자연드림로 12", _HWASUN),    # 주소 검색 표기
    ("화순군 ○○면", _HWASUN),                   # 시도 생략
    ("광주 북구 용봉동", (35.1740, 126.9120)),   # 시도 줄임
    ("서울특별시 강남구 테헤란로 1", None),        # 관외 — '남구' 오탐 금지
    ("경기도 성남시 분당구 판교로 1", None),
]:
    got = geo.coords_of(text)
    check(f"좌표 조회 — {text[:20] or '(빈값)'}", got == want, f"{got} (기대 {want})")

check("시도만 있으면 폴백이고 precise 아님",
      geo.coords_of("전라남도") is not None and not geo.is_precise("전라남도"))

# ── 한글 수사 시각 ────────────────────────────────────────────────
#
# 어르신은 "세시" 라고 말한다. 숫자 정규식만 보던 시절엔 통째로 놓쳐 시각이
# '확인 필요' 로 떨어졌다 — 안전하지만 물어보지 않아도 될 것을 물어보게 된다.
# '한시'는 일부러 뺐다("한시간", "한시라도" 오탐이 크다).
for text, want_time, want_label in [
    ("오후 세시", "15:00", "오후 3시"),
    ("오전 열한시", "11:00", "오전 11시"),
    ("열두시 반", "12:30", "12시 반"),
    ("세시쯤 가야 쓰겄는디", None, "3시"),      # 읽되 오전·오후는 되묻는다
    ("한시간 걸려요", None, None),              # 시각이 아니다
    ("한시라도 빨리", None, None),
]:
    got = parse_time(text) or {}
    ok = got.get("time") == want_time and got.get("label") == want_label
    check(f"한글 수사 — {text[:14]}", ok,
          f"{got.get('time')} / {got.get('label')} (기대 {want_time} / {want_label})")

# ── '다음주 X요일' 은 기준 요일에 흔들리면 안 된다 ──────────────────
#
# (wd - today.weekday()) % 7 은 이미 다가오는 요일로 굴러간다. 거기에 +7 을
# 더하면 그 요일이 이번 주에 지났을 때 **두 주 뒤**가 된다. 기준일이 화요일일
# 때만 우연히 맞아서 오래 안 드러났다 — 어르신이 일주일 늦게 병원에 간다.
_TUE = "2026-08-25"
for off in range(7):                       # 월~일 어느 날 접수해도 같은 화요일
    base = datetime.date(2026, 8, 17) + datetime.timedelta(days=off)
    got = (parse_date("다음주 화요일에 병원 가야", today=base) or {}).get("date")
    check(f"다음주 화요일 — {'월화수목금토일'[base.weekday()]}요일 접수",
          got == _TUE, f"{got} (기대 {_TUE})")

_BASE = datetime.date(2026, 8, 19)         # 수요일
for text, want in [("이번주 금요일에 병원", "2026-08-21"),
                   ("금요일에 병원", "2026-08-21"),
                   ("담주 월요일에 병원", "2026-08-24")]:
    got = (parse_date(text, today=_BASE) or {}).get("date")
    check(f"주 표현 — {text[:10]}", got == want, f"{got} (기대 {want})")

print("=" * 78)
total = 8 + 9 + 6 + 2 + 12 + 2 + 4 + 7 + 6 + 10
print(f"  {total - _fail}/{total} 통과")
sys.exit(1 if _fail else 0)
