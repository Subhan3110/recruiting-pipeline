"""Browser automation for Greenhouse-hosted application forms (job-boards.greenhouse.io).

Field ids on the hosted form equal the field names from the Job Board API
(verified live 2026-11-12: #first_name, #resume, react-select comboboxes at
#question_<id>, checkboxes at #question_<id>[]_<value>). Filling was verified
against a live form without submitting; the post-submit screens (confirmation,
security-code prompt) are implemented from observed patterns and covered by the
local fixture only, so their selectors are deliberately broad.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .. import config
from ..submission import SubmitResult

CONFIRM = re.compile(r"(?i)thank you for applying|application (has been |was )?(submitted|received|sent)|we('ve| have) received your application|"
                     r"thanks for applying|your application is in")
CODE_PROMPT = re.compile(r"(?i)security code|verification code|enter the code|code (was|has been) sent")
ERROR_TEXT = re.compile(r"(?i)this field is required|is required|please (enter|select|complete|attach)|invalid")


def _css_id(field: str) -> str:
    return '[id="' + field.replace('"', '\\"') + '"]'


class GreenhouseFormSubmitter:
    def __init__(self, allowed_hosts: tuple[str, ...], timeout_ms: int = 45000, headless: bool = True):
        self.allowed_hosts, self.timeout, self.headless = allowed_hosts, timeout_ms, headless

    def _host_ok(self, url: str) -> bool:
        u = urlsplit(url)
        return (u.hostname or "") in self.allowed_hosts and (u.scheme == "https" or u.hostname in ("127.0.0.1", "localhost"))

    def submit(self, package: dict, hooks) -> SubmitResult:
        from playwright.sync_api import sync_playwright

        if not self._host_ok(package["url"]):
            return SubmitResult("blocked", blocker="destination", error="form URL is not on an expected host")
        with sync_playwright() as pw:
            proxy = config.https_proxy()
            local = urlsplit(package["url"]).hostname in ("127.0.0.1", "localhost")
            browser = pw.chromium.launch(headless=self.headless, executable_path=config.chromium_path(),
                                         proxy={"server": proxy} if proxy and not local else None)
            try:
                page = browser.new_context(locale="en-GB").new_page()
                page.set_default_timeout(15000)
                return self._run(page, package, hooks)
            finally:
                browser.close()

    # ------------------------------------------------------------------
    def _run(self, page, package: dict, hooks) -> SubmitResult:
        page.goto(package["url"], wait_until="domcontentloaded", timeout=60000)
        if not self._host_ok(page.url):
            return SubmitResult("blocked", blocker="destination", error="form redirected to an unexpected host")
        try:
            page.wait_for_selector(_css_id("first_name"), timeout=20000)
        except Exception:  # noqa: BLE001
            return SubmitResult("blocked", blocker="closed", error="application form not found; the vacancy may have closed",
                                evidence={"url": page.url})
        filled, problems = 0, []
        if package.get("phone_country") and page.locator(_css_id("country")).count():
            try:
                self._combobox(page, "country", package["phone_country"], prefix=True)
            except Exception:  # noqa: BLE001
                problems.append("phone country selector")
        for a in package["answers"]:
            try:
                self._fill(page, a, package)
                filled += 1
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{a['label'][:60]}: {type(exc).__name__}")
        if problems:
            return SubmitResult("blocked", blocker="validation", error="could not fill: " + "; ".join(problems[:6]),
                                evidence={"filled": filled, "url": page.url})
        evidence = {"filled": filled, "form_url": page.url, "documents": {k: f["sha256"][:16] for k, f in package["files"].items()}}
        if getattr(hooks, "mode", "dry_run") != "live":
            return SubmitResult("dry_run", evidence=evidence)
        button = page.locator('button[type="submit"], input[type="submit"]').last
        if not button.count():
            return SubmitResult("blocked", blocker="validation", error="submit button not found", evidence=evidence)
        if not hooks.before_submit():  # final pause/policy check + click journal
            return SubmitResult("not_submitted", error="submission paused at the final check", evidence=evidence)
        button.click()
        state = self._await_outcome(page)
        if state == "code":
            code = hooks.verification_code(f"https://{urlsplit(page.url).hostname}", package["employer"], package["email"],
                                           package["application_id"])
            if not code:
                return SubmitResult("blocked", blocker="verification", evidence=evidence | {"not_sent": True},
                                    error="the portal asked for an e-mailed security code and none could be matched")
            self._enter_code(page, code)
            state = self._await_outcome(page, after_code=True)
            evidence["email_code_used"] = True
        if state == "confirmed":
            text = re.sub(r"\s+", " ", page.inner_text("body"))
            m = CONFIRM.search(text)
            return SubmitResult("confirmed", evidence=evidence | {"kind": "confirmation page", "url": page.url,
                                                                 "text": text[max(0, m.start() - 40):m.end() + 120] if m else ""})
        if state == "errors":
            return SubmitResult("blocked", blocker="validation", error="the form rejected the submission: " + self._errors(page),
                                evidence=evidence | {"not_sent": True})
        if state == "captcha":
            return SubmitResult("blocked", blocker="captcha", error="a CAPTCHA challenge needs you",
                                evidence=evidence | {"not_sent": True})
        return SubmitResult("uncertain", error="no confirmation appeared after submit", evidence=evidence | {"url": page.url})

    def _fill(self, page, a: dict, package: dict):
        field, typ = a["field"], a["type"]
        loc = page.locator(_css_id(field))
        if typ == "input_file":
            f = package["files"][a["key"]]
            loc.set_input_files({"name": f["filename"], "mimeType": f["mime"], "buffer": f["content"]})
            if not loc.evaluate("el => el.files.length === 1"):
                raise RuntimeError("attachment not accepted")
            return
        if typ in ("input_text", "textarea"):
            loc.fill(a["value"])
            if loc.input_value() != a["value"]:
                raise RuntimeError("value did not stick")
            return
        if typ == "multi_value_multi_select":
            values = a["option_value"] if isinstance(a["option_value"], list) else [a["option_value"]]
            boxes = [page.locator(_css_id(f"{field}_{v}")) for v in values]
            if all(b.count() for b in boxes):
                for b in boxes:
                    b.check()
                return
            for label in a["value"].split("; "):
                self._combobox(page, field, label)
            return
        # single select / demographic
        if loc.evaluate("el => el.tagName") == "SELECT":
            loc.select_option(label=a["value"])
            return
        self._combobox(page, field, a["value"])

    def _combobox(self, page, field: str, label: str, prefix: bool = False):
        """react-select: focus, type to filter, pick the exact option, then read the selection back."""
        loc = page.locator(_css_id(field))
        loc.click()
        loc.fill(label[:40])
        opts = page.locator('[role="option"]')
        opts.first.wait_for(timeout=8000)
        pat = re.compile(("^" + re.escape(label)) if prefix else ("^\\s*" + re.escape(label) + "\\s*$"), re.I)
        target = opts.filter(has_text=pat).first
        target.wait_for(timeout=5000)
        chosen = target.inner_text().strip()
        target.click()
        shown = loc.evaluate("""el => { const c = el.closest('[class*="select__control"], [class*="control"]') || el.parentElement.parentElement;
                                        return (c ? c.innerText : '') + ' ' + el.value; }""")
        if chosen.lower()[:25] not in shown.lower():
            raise RuntimeError("selection did not register")

    def _await_outcome(self, page, after_code: bool = False) -> str:
        deadline = self.timeout
        step = 500
        waited = 0
        while waited < deadline:
            page.wait_for_timeout(step)
            waited += step
            try:
                body = page.inner_text("body")
            except Exception:  # noqa: BLE001 - navigation in progress
                continue
            if "/confirmation" in page.url or CONFIRM.search(body):
                return "confirmed"
            if not after_code and (page.locator('input[id^="security-input"], input[name*="security_code"], input[autocomplete="one-time-code"]').count()
                                   or CODE_PROMPT.search(body)):
                return "code"
            if page.locator('[aria-invalid="true"], .error, [class*="error-message"], [id$="-error"]').count() and ERROR_TEXT.search(body):
                return "errors"
            if page.locator('iframe[src*="recaptcha"][src*="bframe"], iframe[title*="challenge"]').locator("visible=true").count():
                return "captcha"
        return "timeout"

    def _enter_code(self, page, code: str):
        cells = page.locator('input[id^="security-input"]')
        if cells.count() >= len(code):
            for i, ch in enumerate(code):
                cells.nth(i).fill(ch)
        else:
            page.locator('input[id^="security-input"], input[name*="security_code"], input[autocomplete="one-time-code"]').first.fill(code)
        page.locator('button[type="submit"], input[type="submit"]').last.click()

    def _errors(self, page) -> str:
        texts = page.locator('[class*="error"], [id$="-error"]').all_inner_texts()
        return "; ".join(sorted({re.sub(r"\s+", " ", t).strip()[:80] for t in texts if t.strip()}))[:400]
