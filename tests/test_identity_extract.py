"""통화에서 말한 성함·사는 곳을 뽑는지 확인한다.

통화에서 "성함과 사시는 읍면동을 말씀해 주세요" 라고 물어놓고 답을 아무 데도
담지 않았다. 물어본 이유가 화면에 없어서, 복지사가 매번 원문을 읽어야 했다.

이 파일이 지키는 두 가지:
  1. 정형 발화에서 이름·주소를 뽑는다.
  2. **못 잡는 것보다 틀리게 잡는 쪽이 나쁘다.** "허리 아파서" 를 주소로,
     "저는 지금 무릎이" 를 이름으로 잡으면 안 된다.
  3. 뽑은 값은 **어떤 경로로도 '확인됨' 이 되지 않는다.**
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from donghaenggori.core import pipeline  # noqa: E402
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

print("=" * 78)
total = 8 + 9 + 6 + 2 + 12 + 2
print(f"  {total - _fail}/{total} 통과")
sys.exit(1 if _fail else 0)
