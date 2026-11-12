"""Local stand-in for a Greenhouse-hosted application form, for repeatable submission tests.

It mirrors the live form's structure (field ids, react-select style comboboxes,
checkbox ids, file inputs). Behaviour is chosen by the path:
  /ok/...     confirmation page after submit
  /code/...   asks for an e-mailed security code (CODE), then confirms
  /hang/...   never answers the submit (ambiguous outcome)
  /error/...  shows validation errors
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CODE = "A1B2C3D4"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Job Application for Analyst at Fixture Bank</title></head><body>
<form id="application-form">
<input id="first_name" aria-label="First Name"><input id="last_name"><input id="email"><input id="phone" type="tel">
<div class="select__control"><div class="select__single-value"></div><input id="country" role="combobox" data-options='["Norway +47","Sweden +46","United States +1"]'></div>
<input id="resume" type="file"><input id="cover_letter" type="file">
<div class="select__control"><div class="select__single-value"></div><input id="question_1" role="combobox" data-options='["Yes","No"]'></div>
<div class="select__control"><div class="select__single-value"></div><input id="question_2" role="combobox" data-options='["Yes","No"]'></div>
<div class="select__control"><div class="select__single-value"></div><input id="question_3" role="combobox" data-options='["Company website","LinkedIn","Other"]'></div>
<input type="checkbox" id="question_4[]_11" name="question_4[]" value="11"><input type="checkbox" id="question_4[]_12" name="question_4[]" value="12">
<textarea id="question_5"></textarea>
<div id="errors"></div>
<button type="submit">Submit application</button>
</form>
<div id="listbox"></div>
<script>
const MODE = location.pathname.split('/')[1];
document.querySelectorAll('input[role=combobox]').forEach(inp => {
  const show = () => { const lb = document.getElementById('listbox'); lb.innerHTML = '';
    JSON.parse(inp.dataset.options).filter(o => o.toLowerCase().includes(inp.value.toLowerCase())).forEach(o => {
      const d = document.createElement('div'); d.setAttribute('role', 'option'); d.textContent = o;
      d.onclick = () => { inp.parentElement.querySelector('.select__single-value').textContent = o; inp.dataset.chosen = o; inp.value = ''; lb.innerHTML = ''; };
      lb.appendChild(d); }); };
  inp.addEventListener('focus', show); inp.addEventListener('input', show); inp.addEventListener('click', show);
});
const collect = () => { const out = {};
  document.querySelectorAll('form input, form textarea').forEach(e => {
    if (e.type === 'file') out[e.id] = e.files.length ? e.files[0].name + ':' + e.files[0].size : null;
    else if (e.type === 'checkbox') { if (e.checked) (out[e.name] = out[e.name] || []).push(e.value); }
    else if (e.getAttribute('role') === 'combobox') out[e.id] = e.dataset.chosen || null;
    else out[e.id] = e.value; });
  return out; };
document.getElementById('application-form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const data = collect();
  if (MODE === 'error') { document.getElementById('errors').innerHTML = '<p class="error" id="email-error">This field is required</p>'; return; }
  if (MODE === 'hang') { await fetch('/submit', {method: 'POST', body: JSON.stringify(data)}); return; }
  if (MODE === 'code' && !window.codeStage) { window.codeStage = true; await fetch('/code-requested', {method: 'POST', body: '{}'});
    const box = document.createElement('div'); box.innerHTML = '<p>Enter the security code sent to your email</p><input id="security-input-0">';
    document.getElementById('application-form').insertBefore(box, document.querySelector('button')); return; }
  if (MODE === 'code') { const c = document.getElementById('security-input-0').value; data.security_code = c;
    if (c !== '""" + CODE + """') { document.getElementById('errors').innerHTML = '<p class="error">Invalid code. This field is required</p>'; return; } }
  await fetch('/submit', {method: 'POST', body: JSON.stringify(data)});
  document.body.innerHTML = '<h1>Thank you for applying</h1><p>Your application has been received.</p>';
});
</script></body></html>"""


class FormServer:
    def __init__(self):
        self.submissions: list[dict] = []
        self.code_requests = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/submit":
                    outer.submissions.append(json.loads(raw or b"{}"))
                else:
                    outer.code_requests += 1
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, mode: str) -> str:
        return f"http://127.0.0.1:{self.port}/{mode}/fixturebank/jobs/1"

    def close(self):
        self.httpd.shutdown()
