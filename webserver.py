#!/usr/bin/env python3
"""동행고리 AI — 사회복지사용 웹 대시보드 (의존성 0, 표준 라이브러리만 + SQLite).

실행:  python3 webserver.py   →  http://localhost:8765
  · 접수: 발신번호+발화 → 접수카드
  · ③ 확정·배정 → 접수 기록(SQLite) 저장
  · 🔁 동행 완료 → 이력 추가(플라이휠) → 다음 접수가 더 정확해짐
  · 접수 목록 / 초기화
Streamlit 없이 Python 표준 http.server + sqlite3 만으로 동작.
"""
from __future__ import annotations

import datetime
import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from donghaenggori import db, pipeline

PORT = 8765

EXAMPLES = [
    ("단골 (확인됨)", "010-1234-5678", "모레 정형외과 가야겄어"),
    ("플라이휠 (추정→확인됨)", "010-2222-3333", "허리 아파서 정형외과 가야 하는디"),
    ("신규 (확인 필요)", "010-7777-8888", "다음주 화요일에 병원 가야 하는디"),
    ("긴급 (사람 연결)", "010-1234-5678", "가슴이 아파서 숨이 차"),
]

STATUS_BADGE = {"확인됨": ("🟢", "#1a7f37"), "추정": ("🟡", "#b58105"), "확인 필요": ("🔴", "#cf222e")}

CSS = """
*{box-sizing:border-box} body{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;
background:#f4f1ea;margin:0;color:#1f2328;line-height:1.55}
.wrap{max-width:760px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:26px;margin:0 0 4px} .sub{color:#57606a;margin:0 0 16px}
.nav a{display:inline-block;margin-right:6px;padding:6px 14px;border:1px solid #d0d7de;border-radius:20px;
background:#fff;color:#1f2328;text-decoration:none;font-size:13px}
.mode{display:inline-block;background:#fff;border:1px solid #d0d7de;border-radius:20px;
padding:4px 12px;font-size:13px;margin:14px 0 8px}
.banner{background:#dafbe1;border:1px solid #8fce9c;color:#116329;border-radius:10px;padding:10px 14px;margin:12px 0;font-size:14px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:14px;padding:20px;margin:16px 0;
box-shadow:0 1px 3px rgba(0,0,0,.06)}
.sec{font-size:13px;font-weight:700;color:#bc5b2e;letter-spacing:.04em;margin:0 0 10px}
form.intake{display:flex;gap:8px;flex-wrap:wrap}
input[type=text]{flex:1;min-width:150px;padding:10px 12px;border:1px solid #d0d7de;border-radius:10px;font-size:15px}
button{background:#bc5b2e;color:#fff;border:0;border-radius:10px;padding:10px 18px;font-size:15px;cursor:pointer}
button.ghost{background:#fff;color:#bc5b2e;border:1px solid #bc5b2e}
.ex a{display:inline-block;background:#fff;border:1px solid #d0d7de;border-radius:18px;
padding:5px 12px;margin:8px 6px 0 0;font-size:13px;color:#1f2328;text-decoration:none}
.metrics{display:flex;gap:10px;margin:6px 0 14px;flex-wrap:wrap}
.metric{flex:1;min-width:150px;background:#faf8f3;border:1px solid #eadfce;border-radius:10px;padding:12px}
.metric .k{font-size:12px;color:#57606a} .metric .v{font-size:18px;font-weight:700;margin-top:2px}
.badge{font-size:13px;font-weight:700} .flag{background:#fff3cd;border:1px solid #f0c36d;color:#7a5b00;
border-radius:8px;padding:8px 12px;margin:0 0 12px;font-size:14px}
.urgent{background:#ffe9e9;border:1px solid #e5534b;color:#a40e26;border-radius:12px;padding:18px;font-size:16px;font-weight:600}
.kv{margin:6px 0} .kv b{color:#1f2328} .reason{color:#3a4149}
ul.q{margin:6px 0 0;padding-left:18px} li{margin:3px 0}
.confirm{background:#faf8f3;border:1px dashed #c9b89a;border-radius:10px;padding:14px;margin-top:14px}
.confirm .row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;align-items:center}
.confirm label{font-size:12px;color:#57606a;min-width:74px}
.note{color:#57606a;font-size:13px;margin-top:8px}
table{width:100%;border-collapse:collapse;font-size:14px} th,td{border-bottom:1px solid #eee;padding:8px 6px;text-align:left}
th{color:#57606a;font-weight:600;font-size:12px}
.foot{color:#8a8a8a;font-size:12px;margin-top:24px;text-align:center}
"""


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _page(inner: str) -> bytes:
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>동행고리 AI</title><style>{CSS}</style></head><body>"
            f"<div class='wrap'>{inner}"
            f"<p class='foot'>⚠ 시연용 합성 데이터 · AI는 의료/응급 판단을 하지 않으며 최종 확인은 사회복지사가 합니다.</p>"
            f"</div></body></html>").encode("utf-8")


def _header(msg: str | None) -> str:
    llm = "규칙 + Claude(LLM)" if os.environ.get("ANTHROPIC_API_KEY") else "규칙 기반 (키 설정 시 Claude 강화)"
    banner = f"<div class='banner'>✓ {esc(msg)}</div>" if msg else ""
    return (f"<h1>🩺 동행고리 AI</h1>"
            f"<p class='sub'>사회복지사를 위한 병원동행 접수·이력정리 Copilot — AI는 후보·근거만, 확정은 사람</p>"
            f"<div class='nav'><a href='/'>접수</a><a href='/intakes'>접수 목록</a>"
            f"<a href='/reset'>초기화</a></div>"
            f"<span class='mode'>🧠 발화 이해: {esc(llm)}</span>{banner}")


def render_home(phone: str, utterance: str, msg: str | None = None) -> bytes:
    ex = "".join(
        f'<a href="/?phone={urllib.parse.quote(p)}&utterance={urllib.parse.quote(u)}">{esc(label)}</a>'
        for label, p, u in EXAMPLES)
    parts = [_header(msg), f"""
  <div class="card">
    <p class="sec">① 접수 — 어르신 전화 발화</p>
    <form class="intake" method="get" action="/">
      <input type="text" name="phone" value="{esc(phone)}" placeholder="발신번호">
      <input type="text" name="utterance" value="{esc(utterance)}" placeholder="발화(STT 결과) 예: 모레 정형외과 가야겄어">
      <button type="submit">접수카드 생성</button>
    </form>
    <div class="ex">{ex}</div>
  </div>"""]

    if utterance.strip():
        res = pipeline.run(phone, utterance)
        parts.append(f'<div class="card"><p class="sec">② 사회복지사용 접수카드 '
                     f'<span style="color:#8a8a8a;font-weight:400">(발화 분석: {esc(res.analysis.source)})</span></p>')
        if res.urgent:
            parts.append(f'<div class="urgent">🚨 {esc(res.urgent_message)}</div></div>')
        else:
            parts.append(_card_html(phone, utterance, res.card))
            parts.append("</div>")
    return _page("".join(parts))


def _card_html(phone, utterance, c) -> str:
    icon, color = STATUS_BADGE.get(c.hospital_status, ("⚪", "#57606a"))
    flag = ("<div class='flag'>⚠️ " + "　".join(esc(f) for f in c.flags) + "</div>") if c.flags else ""
    q = ("<div class='kv'><b>확인 질문(콜백 스크립트)</b><ul class='q'>"
         + "".join(f"<li>{esc(x)}</li>" for x in c.confirm_questions) + "</ul></div>") if c.confirm_questions else ""
    hidden = (f"<input type='hidden' name='phone' value='{esc(phone)}'>"
              f"<input type='hidden' name='utterance' value='{esc(utterance)}'>")
    today = datetime.date.today().isoformat()
    return f"""
    {flag}
    <div class="metrics">
      <div class="metric"><div class="k">대상자</div><div class="v">{esc(c.target.split('(')[0])}</div></div>
      <div class="metric"><div class="k">병원 후보</div><div class="v">{esc(c.hospital or '—')}
        <span class="badge" style="color:{color}">{icon} {esc(c.hospital_status)}</span></div></div>
      <div class="metric"><div class="k">방문 예정</div><div class="v">{esc(c.date_label or '미확정')}</div></div>
    </div>
    <div class="kv"><b>원문 발화</b> : “{esc(c.raw_utterance)}”</div>
    <div class="kv"><b>AI 요약</b> : {esc(c.summary)}</div>
    <div class="kv"><b>진료과</b> : {esc(c.dept or '—')} ｜ <b>요청 유형</b> : {esc(c.intent)}</div>
    <div class="kv reason"><b>근거</b> : {esc(' / '.join(c.reasons)) or '—'}</div>
    {q}
    <div class="kv"><b>동행 지원 수준(후보)</b> : {esc(c.need_level)}
      <span class="note">({esc(', '.join(c.need_reasons))})</span></div>
    <div class="kv"><b>보호자 연락 필요</b> : {'예' if c.guardian_contact else '아니오'}</div>
    <div class="kv"><b>매니저 전달</b> : {esc(' / '.join(c.manager_notes)) or '—'}</div>

    <div class="confirm">
      <p class="sec" style="color:#57606a">③ 사회복지사 확인·확정 (사람의 영역)</p>
      <form method="post" action="/confirm">{hidden}
        <div class="row"><label>병원명 확정</label>
          <input type="text" name="c_hospital" value="{esc(c.hospital or '')}"></div>
        <div class="row"><label>방문일 확정</label>
          <input type="text" name="c_date" value="{esc(c.date_value or '')}"></div>
        <div class="row"><label>동행 수준</label>
          <input type="text" name="c_level" value="{esc(c.need_level)}">
          <button type="submit">확정·배정 저장</button></div>
      </form>
      <form method="post" action="/complete" style="margin-top:6px">{hidden}
        <div class="row"><label>🔁 동행 완료</label>
          <input type="text" name="h_hospital" value="{esc(c.hospital or '')}" placeholder="실제 방문 병원">
          <input type="text" name="h_dept" value="{esc(c.dept or '')}" placeholder="진료과">
          <input type="hidden" name="h_date" value="{today}">
          <button type="submit" class="ghost">이력에 추가</button></div>
        <div class="note">동행을 완료 처리하면 과거 이력에 쌓여, <b>다음 접수부터 이 어르신의 병원 후보가 더 정확</b>해집니다(학습·플라이휠).</div>
      </form>
      <div class="note">※ 병원명·일정·등급은 사회복지사가 최종 확인·확정합니다.</div>
    </div>"""


def render_intakes(msg: str | None = None) -> bytes:
    rows = db.list_intakes()
    if rows:
        body = "<table><tr><th>시각</th><th>대상자</th><th>발화</th><th>병원 후보</th><th>상태</th><th>확정</th></tr>"
        for r in rows:
            conf = f"✅ {esc(r['confirmed_hospital'])}" if r["confirmed"] else "—"
            body += (f"<tr><td>{esc(r['created_at'])}</td><td>{esc(r['target'].split('(')[0])}</td>"
                     f"<td>{esc(r['raw_utterance'])}</td><td>{esc(r['hospital'] or '—')}</td>"
                     f"<td>{esc(r['hospital_status'])}</td><td>{conf}</td></tr>")
        body += "</table>"
    else:
        body = "<p class='note'>아직 저장된 접수가 없습니다. 접수카드에서 '확정·배정 저장'을 눌러보세요.</p>"
    return _page(_header(msg) + f"<div class='card'><p class='sec'>접수 목록</p>{body}</div>")


class Handler(BaseHTTPRequestHandler):
    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _send(self, page: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        msg = q.get("msg", [None])[0]
        if u.path in ("/", "/index.html"):
            self._send(render_home(q.get("phone", ["010-1234-5678"])[0],
                                   q.get("utterance", [""])[0], msg))
        elif u.path == "/intakes":
            self._send(render_intakes(msg))
        elif u.path == "/reset":
            db.reset_db()
            self._redirect("/?msg=" + urllib.parse.quote("DB를 초기화했습니다(시드 데이터 복원)"))
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        g = lambda k: form.get(k, [""])[0]
        phone, utterance = g("phone"), g("utterance")

        if self.path == "/confirm":
            res = pipeline.run(phone, utterance)
            if res.card:
                iid = db.save_intake(res.card, phone)
                db.confirm_intake(iid, g("c_hospital"), g("c_date"), g("c_level"))
            self._redirect("/intakes?msg=" + urllib.parse.quote("접수를 확정·저장했습니다"))
        elif self.path == "/complete":
            db.add_history(phone, g("h_date") or datetime.date.today().isoformat(),
                           g("h_hospital"), g("h_dept"), source="사후메모")
            back = f"/?phone={urllib.parse.quote(phone)}&utterance={urllib.parse.quote(utterance)}" \
                   f"&msg={urllib.parse.quote('동행 완료를 이력에 추가했습니다 — 같은 접수를 다시 생성해 보세요(상태가 올라갑니다)')}"
            self._redirect(back)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    db.init_db()
    print(f"동행고리 AI 대시보드 → http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
