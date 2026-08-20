"""이동지원·보호자를 발화에서 뽑는지 검증 — 프로필 값이 섞이지 않는지까지.

실행:  PYTHONPATH=. python -m tests.test_mobility

성능평가에서 드러난 것을 막는다. 어르신이 "나 혼자 갈 수 있어" 라고 말해도
카드는 프로필 등급으로 "휠체어·부축 동행" 을 내놓았고, 그 말이 카드에 남지
않았다. 두 값을 나란히 두되 **출처가 섞이지 않는 것**이 이 테스트의 목적이다.

방언 표현은 팀 사전(docs/eval/전남방언_매핑사전.xlsx)의 항목을 그대로 쓴다 —
'혼자 가긴 좀 그런디', '삭신이 쑤신다', '우리 아들내미', '못 가것다'.
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="mobility-test-"), "test.db")
os.environ["DONGHAENGGORI_DB"] = _TMP_DB

from donghaenggori.core import db, pipeline  # noqa: E402
from donghaenggori.core import mobility as mb  # noqa: E402

PHONE = "010-1234-5678"        # 박순자 — 프로필에 장기요양등급·보호자가 있다

results: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


# ------------------------------------------------------- 판정 --

def test_verdicts() -> None:
    cases = [
        # (발화, 기대 판정, 기대 필요여부, 기대 상태)
        ("나 혼자 갈 수 있어, 도움 필요없어", mb.EXPLICIT_NO_NEED, "불필요", "확인됨"),
        ("다리가 불편한데 가족들은 멀리 살고, 사람이 필요해",
         mb.EXPLICIT_NEED, "필요", "확인됨"),
        ("삭신이 다 쑤셔서 못 가것다, 우리 아들내미가 델러다 줄틴디",
         mb.FAMILY_SUPPORT, "불필요", "확인됨"),
        ("버스 타고 혼자 가긴 좀 그런디", mb.IMPLICIT_NEED, "필요", "추정"),
        ("모레 정형외과 가야겄어", mb.NO_SIGNAL, None, "확인 필요"),
    ]
    for text, verdict, need, status in cases:
        m = mb.extract_mobility_need(text)
        check(f"{verdict} — {text[:18]}…", m.verdict == verdict, f"→ {m.verdict}")
        check(f"  필요여부 {need}", m.need == need, f"→ {m.need}")
        check(f"  상태 {status}", m.status == status, f"→ {m.status}")
        check(f"  근거가 남는다 — {text[:12]}…", bool(m.evidence), str(m.evidence))

    # 근거는 **원문 문구 그대로**여야 한다. 요약하거나 바꿔 적으면 "왜 그렇게
    # 판정했나" 에 거짓으로 답하게 된다.
    m = mb.extract_mobility_need("버스 타고 혼자 가긴 좀 그런디")
    quoted = [e.split("'")[1] for e in m.evidence if "'" in e]
    check("근거가 원문에 실제로 있다",
          all(q in "버스 타고 혼자 가긴 좀 그런디" for q in quoted), str(quoted))


def test_priority() -> None:
    """어르신이 직접 밝힌 말이 우리 추론을 이긴다."""
    m = mb.extract_mobility_need("다리가 불편하지만 혼자 갈 수 있어요")
    check("직접 말한 것이 추론보다 우선", m.verdict == mb.EXPLICIT_NO_NEED, m.verdict)

    # 가족지원과 명시적 불필요는 **다른 판정**이다. 근거가 다르고, 가족이 못
    # 오게 되면 바로 필요해진다 — 복지사가 그 차이를 알아야 한다.
    fam = mb.extract_mobility_need("우리 아들내미가 델러다 줄틴디")
    self_ok = mb.extract_mobility_need("나 혼자 갈 수 있어")
    check("가족지원과 본인 거절을 구분한다",
          fam.verdict != self_ok.verdict and fam.need == self_ok.need,
          f"{fam.verdict} vs {self_ok.verdict}")
    check("가족이 못 오면 필요해진다는 것을 근거에 남긴다",
          any("바로 필요해진다" in e for e in fam.evidence), str(fam.evidence))


def test_mixed_priority() -> None:
    """암시적 필요와 가족지원이 한 문장에 같이 나오면 **가족지원이 이긴다.**

    팀 승인된 우선순위다 — 가족이 온다면 우리 동행은 불필요하고, 근거에
    "가족이 못 오게 되면 바로 필요해진다" 가 남아 복지사가 그 위험을 본다.

    순서: ① 명시적_불필요 ② 가족지원있음 ③ 명시적_필요 ④ 암시적_필요 ⑤ 신호없음
    """
    m = mb.extract_mobility_need("다리가 불편한데 우리 아들이 데려다 준다고 했어")
    check("암시 + 가족지원 → 가족지원이 이긴다", m.verdict == mb.FAMILY_SUPPORT, m.verdict)
    check("그때도 불필요·확정", (m.need, m.status) == ("불필요", "확인됨"),
          f"{m.need} [{m.status}]")
    check("가족이 못 오면 필요해진다는 경고가 남는다",
          any("바로 필요해진다" in e for e in m.evidence), str(m.evidence))

    # 본인이 직접 괜찮다고 하면 그것이 가족지원보다 앞선다 — 당사자의 말이다.
    m2 = mb.extract_mobility_need("아들이 데려다 준다는데 나 혼자 갈 수 있어")
    check("본인 말이 가족지원보다 앞선다", m2.verdict == mb.EXPLICIT_NO_NEED, m2.verdict)


def test_guardian() -> None:
    g = mb.extract_guardian_info("삭신이 다 쑤셔서 못 가것다, 우리 아들내미가 델러다 줄틴디")
    check("가족이 데려다주면 그렇게 적는다",
          g.verdict == mb.MENTIONED and "데려다줄" in (g.content or ""), str(g.content))

    # "가족들은 멀리 살고" 를 '보호자 있음' 으로 적으면 복지사가 연락하려 한다.
    g2 = mb.extract_guardian_info("다리가 불편한데 가족들은 멀리 살고, 사람이 필요해")
    check("도움받기 어려운 맥락을 구분한다",
          "어려움" in (g2.content or ""), str(g2.content))
    check("연락 대상으로 쓰지 말라고 남긴다",
          any("연락 대상으로 쓰지" in e for e in g2.evidence), str(g2.evidence))

    g3 = mb.extract_guardian_info("모레 정형외과 가야겄어")
    check("언급이 없으면 확인 필요", g3.verdict == mb.NO_SIGNAL and g3.content is None,
          str(g3.to_dict()))
    check("프로필을 쓰지 않는다고 남긴다",
          any("프로필" in e for e in g3.evidence), str(g3.evidence))


def test_no_profile_access() -> None:
    """**발화 텍스트만** 입력이다. 같은 문장이면 누가 걸어도 같은 결과여야 한다."""
    text = "나 혼자 갈 수 있어, 도움 필요없어"
    a = mb.extract_mobility_need(text).to_dict()
    b = mb.extract_mobility_need(text).to_dict()
    check("같은 발화 → 같은 결과", a == b, "")
    # 함수 시그니처에 전화번호·프로필이 없다는 것 자체가 계약이다.
    import inspect
    sig = inspect.signature(mb.extract_mobility_need)
    check("입력이 발화 하나뿐", list(sig.parameters) == ["utterance"], str(sig))
    sig2 = inspect.signature(mb.extract_guardian_info)
    check("보호자도 입력이 발화 하나뿐", list(sig2.parameters) == ["utterance"], str(sig2))


# ------------------------------------------------------- 카드 --

def test_card_keeps_both() -> None:
    """프로필 기반과 발화 기반이 **나란히** 있어야 한다. 덮어쓰지 않는다."""
    res = pipeline.run(PHONE, "나 혼자 갈 수 있어, 도움 필요없어",
                       use_llm=False, with_rag=False)
    c = res.card.to_dict()
    check("프로필 기반 지원 수준이 남아 있다", bool(c["need_level"]), str(c["need_level"]))
    check("발화 기반 판정이 따로 있다",
          c["mobility_need"]["필요여부"] == "불필요", str(c["mobility_need"]))
    check("두 값이 다른 키다", c["need_level"] != c["mobility_need"].get("필요여부"),
          f"{c['need_level']} / {c['mobility_need'].get('필요여부')}")

    # 언급이 없으면 프로필 값을 이 칸에 쓰지 않는다.
    c2 = pipeline.run(PHONE, "모레 정형외과 가야겄어",
                      use_llm=False, with_rag=False).card.to_dict()
    check("언급 없으면 확인 필요",
          c2["mobility_need"]["상태"] == "확인 필요"
          and c2["mobility_need"]["필요여부"] is None, str(c2["mobility_need"]))
    check("그래도 프로필 지원 수준은 그대로", bool(c2["need_level"]), str(c2["need_level"]))

    # 보호자도 같다 — 프로필 연락처와 통화에서 들은 말을 나눈다.
    c3 = pipeline.run(PHONE, "우리 아들내미가 델러다 줄틴디",
                      use_llm=False, with_rag=False).card.to_dict()
    check("프로필 보호자 연락처가 남아 있다", bool(c3["guardian"]), str(c3["guardian"]))
    check("통화에서 들은 보호자가 따로 있다",
          "데려다줄" in (c3["guardian_mentioned"].get("내용") or ""),
          str(c3["guardian_mentioned"]))


# --------------------------------------------- 방언(팀 사전) --

def test_dialect() -> None:
    """팀 사전의 방언 표현이 실제로 잡히는지."""
    cases = [
        ("혼자 가긴 좀 그런디", mb.IMPLICIT_NEED),         # 이동/지원 암시
        ("삭신이 다 쑤셔서 못 가것다", mb.IMPLICIT_NEED),   # 신체/건강
        ("우리 아들내미가 델러다 줄틴디", mb.FAMILY_SUPPORT),  # 가족/호칭
    ]
    for text, want in cases:
        got = mb.extract_mobility_need(text).verdict
        check(f"방언 — {text[:14]}…", got == want, f"→ {got}")


def main() -> int:
    db.init_db(force=True)
    test_verdicts()
    test_priority()
    test_mixed_priority()
    test_guardian()
    test_no_profile_access()
    test_card_keeps_both()
    test_dialect()

    print("\n이동지원·보호자 발화 추출 검증")
    print("=" * 78)
    passed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:44s} {detail}")
        passed += ok
    print("=" * 78)
    print(f"  {passed}/{len(results)} 통과   (DB: {_TMP_DB})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
