"""E-mail verification and acknowledgement matching for pending recruitment workflows.

Rules
- A message is only considered for a *known pending request*: right recipient, expected sender
  domain, received after the request (minus small clock skew) and before it expires.
- With several pending requests on the same portal and mailbox, a message must name the request's
  employer; otherwise it is left alone rather than guessed.
- Links are returned only when every hop stays on the portal's own hosts over https.
- Message text is data. Nothing in it is executed, obeyed or forwarded. Codes are never stored or logged.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import requests

from . import policy as policy_mod, profile
from .db import Conn, j, new_id, now
from .employers import normalise
from .mail.base import MailMessage, MailProvider

SKEW = timedelta(seconds=90)
CODE_NEAR = re.compile(r"(?i)\b(?:code|kode|passcode|pin|otp)\b[^A-Za-z0-9]{1,6}(?:(?:is|er|:)\s*)?[^A-Za-z0-9]{0,40}?\b((?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,10})\b")
CODE_LINE = re.compile(r"(?m)^\s*((?=[A-Za-z0-9]*\d)[A-Z0-9]{6,10}|\d{4,8})\s*$")
URL = re.compile(r"https?://[^\s\"'<>)\]]+")
VERIFY_PATH = re.compile(r"(?i)verif|confirm|activat|validat|token|registration")
ACK = re.compile(r"(?i)thank you for (applying|your application|your interest)|application (has been |was )?(received|submitted)|"
                 r"(we|we've|we have) received your application|takk for (din |)søknad(en)?|vi har mottatt (din )?søknad|successfully applied")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def open_request(conn: Conn, purpose: str, portal_origin: str, employer_hint: str, recipient: str,
                 application_id: str | None = None, account_id: str | None = None) -> str:
    _, cfg = policy_mod.current(conn)
    rid = new_id("vr")
    t = datetime.now(timezone.utc)
    conn.q("INSERT INTO verification_requests (id, purpose, portal_origin, employer_hint, recipient, application_id, account_id, requested_at, expires_at) "
           "VALUES (?,?,?,?,?,?,?,?,?)", (rid, purpose, portal_origin, employer_hint, recipient.lower(), application_id, account_id, _iso(t),
                                         _iso(t + timedelta(minutes=cfg["verification"]["code_ttl_minutes"]))))
    conn.event("verification.requested", "verification", rid, {"portal": portal_origin, "purpose": purpose})
    return rid


def resend_allowed(conn: Conn, request_id: str) -> bool:
    """Bounded resends. Registers the resend and restarts the window so only newer messages count."""
    _, cfg = policy_mod.current(conn)
    r = conn.one("SELECT * FROM verification_requests WHERE id=?", (request_id,))
    if not r or r["status"] not in ("pending", "expired") or r["resend_count"] >= cfg["verification"]["max_resends"]:
        if r and r["status"] != "consumed":
            conn.q("UPDATE verification_requests SET status='failed', resolved_at=? WHERE id=?", (now(), request_id))
        return False
    t = datetime.now(timezone.utc)
    conn.q("UPDATE verification_requests SET resend_count=resend_count+1, status='pending', requested_at=?, expires_at=? WHERE id=?",
           (_iso(t), _iso(t + timedelta(minutes=cfg["verification"]["code_ttl_minutes"])), request_id))
    return True


def extract_code(msg: MailMessage) -> str | None:
    text = msg.text or re.sub(r"<[^>]+>", " ", htmllib.unescape(msg.html or ""))
    m = CODE_NEAR.search(text) or CODE_NEAR.search(msg.subject or "") or CODE_LINE.search(text)
    return m.group(1) if m else None


def host_allowed(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and not u.username and bool(host) and any(host == h or host.endswith("." + h) for h in allowed_hosts)


def extract_link(msg: MailMessage, allowed_hosts: tuple[str, ...]) -> str | None:
    """A verification link on the portal's own hosts, or None. Look-alike and off-portal links are ignored."""
    body = (msg.text or "") + " " + htmllib.unescape(msg.html or "")
    for url in URL.findall(body):
        url = url.rstrip(".,;")
        if host_allowed(url, allowed_hosts) and VERIFY_PATH.search(urlsplit(url).path + "?" + urlsplit(url).query):
            return url
    return None


def safe_follow(url: str, allowed_hosts: tuple[str, ...], session: requests.Session | None = None, max_hops: int = 5) -> str:
    """Resolve redirects one hop at a time; refuse the moment a hop leaves the allowed hosts."""
    s = session or requests.Session()
    for _ in range(max_hops):
        if not host_allowed(url, allowed_hosts):
            raise PermissionError(f"link leaves the expected portal: {urlsplit(url).hostname}")
        r = s.get(url, allow_redirects=False, timeout=30)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            continue
        return url
    raise PermissionError("too many redirects")


def _mentions(msg: MailMessage, hint: str) -> bool:
    hay = normalise(f"{msg.subject} {msg.text[:4000]} {re.sub(r'<[^>]+>', ' ', msg.html[:8000])}")
    return bool(hint) and normalise(hint) in hay


def poll(conn: Conn, provider: MailProvider, request_id: str, sender_domains: tuple[str, ...], allowed_hosts: tuple[str, ...]) -> dict:
    """One look at the mailbox for one pending request -> {'status': 'code'|'link'|'waiting'|'expired'|'done', ...}"""
    r = conn.one("SELECT * FROM verification_requests WHERE id=?", (request_id,))
    if r["status"] != "pending":
        return {"status": "done" if r["status"] == "consumed" else r["status"]}
    since = _iso(datetime.fromisoformat(r["requested_at"]) - SKEW)
    rivals = conn.q("SELECT employer_hint FROM verification_requests WHERE status='pending' AND portal_origin=? AND recipient=? AND id<>?",
                    (r["portal_origin"], r["recipient"], request_id))
    used = {m["message_ref"] for m in conn.q("SELECT message_ref FROM email_messages WHERE provider=?", (provider.name,))}
    best = None
    for msg in provider.search(since, r["recipient"], sender_domains):
        if msg.ref in used or msg.received_at > r["expires_at"] or msg.received_at < since:
            continue
        if rivals:
            if not _mentions(msg, r["employer_hint"] or ""):
                continue  # cannot tell which workflow this belongs to: never guess
            if any(h["employer_hint"] and h["employer_hint"] != r["employer_hint"] and _mentions(msg, h["employer_hint"]) for h in rivals):
                continue
        code, link = extract_code(msg), extract_link(msg, allowed_hosts)
        if code or link:
            best = (msg, code, link)  # search() is oldest-first: the newest valid message wins
    if best:
        msg, code, link = best
        with conn.tx():
            conn.q("UPDATE verification_requests SET status='consumed', message_ref=?, resolved_at=? WHERE id=? AND status='pending'",
                   (msg.ref, now(), request_id))
            _record(conn, provider, msg, "verification", r["application_id"], request_id)
            conn.event("verification.matched", "verification", request_id, {"message_ref": msg.ref, "kind": "code" if code else "link"})
        return {"status": "code", "code": code, "message_ref": msg.ref} if code else {"status": "link", "url": link, "message_ref": msg.ref}
    if now() > r["expires_at"]:
        conn.q("UPDATE verification_requests SET status='expired', resolved_at=? WHERE id=? AND status='pending'", (now(), request_id))
        return {"status": "expired"}
    return {"status": "waiting"}


def _record(conn: Conn, provider: MailProvider, msg: MailMessage, kind: str, application_id: str | None, request_id: str | None):
    """Keep a message reference and minimal evidence only: no body, no code, no link."""
    conn.q("INSERT INTO email_messages (id, provider, message_ref, received_at, sender_domain, kind, application_id, verification_request_id, evidence, created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (provider, message_ref) DO NOTHING",
           (new_id("em"), provider.name, msg.ref, msg.received_at, msg.sender_domain, kind, application_id, request_id,
            j({"subject_sha256": hashlib.sha256((msg.subject or "").encode()).hexdigest()[:16], "sender_domain": msg.sender_domain}), now()))


def make_verifier(conn: Conn, provider: MailProvider | None, sender_domains: tuple[str, ...], allowed_hosts: tuple[str, ...],
                  wait_seconds: int | None = None, interval: float = 10, sleep=time.sleep):
    """Blocking helper for the browser flow: waits for a code for this application, allowing for delivery delay."""
    def verifier(portal_origin: str, employer_hint: str, recipient: str, application_id: str) -> str | None:
        if provider is None:
            profile.open_task(conn, "manual_action", "Connect a mailbox for verification codes",
                              f"{employer_hint} sent a verification code but no mailbox is connected.", dedup_key="mail:connect")
            return None
        _, cfg = policy_mod.current(conn)
        rid = open_request(conn, "application_code", portal_origin, employer_hint, recipient, application_id)
        deadline = time.monotonic() + (wait_seconds if wait_seconds is not None else cfg["verification"]["poll_seconds"])
        while time.monotonic() < deadline:
            res = poll(conn, provider, rid, sender_domains, allowed_hosts)
            if res["status"] == "code":
                return res["code"]
            if res["status"] in ("expired", "failed", "link"):
                break
            sleep(interval)
        conn.q("UPDATE verification_requests SET status='expired', resolved_at=? WHERE id=? AND status='pending'", (now(), rid))
        return None
    return verifier


def reconcile_acknowledgements(conn: Conn, provider: MailProvider, sender_domains: tuple[str, ...], days: int = 21) -> list[str]:
    """Match acknowledgement e-mails to uncertain (and recently submitted) applications and record evidence."""
    from . import submission

    done = []
    rows = conn.q("SELECT a.id, a.state, a.role_title, e.name, e.domain, MIN(t.started_at) AS started, "
                  "MAX(CASE WHEN t.outcome IN ('uncertain','confirmed') THEN t.id END) AS attempt_id "
                  "FROM applications a JOIN employers e ON e.id=a.employer_id JOIN attempts t ON t.application_id=a.id "
                  "WHERE a.state IN ('uncertain','submitted') AND t.submit_clicked_at IS NOT NULL AND t.started_at > ? "
                  "GROUP BY a.id, a.state, a.role_title, e.name, e.domain", (_iso(datetime.now(timezone.utc) - timedelta(days=days)),))
    if not rows:
        return done
    recipient = profile.get_fact(conn, "contact/application_email") or profile.get_fact(conn, "contact/email")
    used = {m["message_ref"] for m in conn.q("SELECT message_ref FROM email_messages WHERE provider=?", (provider.name,))}
    for app in rows:
        if conn.one("SELECT 1 FROM email_messages WHERE application_id=? AND kind='acknowledgement'", (app["id"],)):
            continue
        domains = tuple(sender_domains) + ((app["domain"],) if app["domain"] else ())
        since = _iso(datetime.fromisoformat(app["started"]) - SKEW)
        for msg in provider.search(since, recipient, domains):
            text = f"{msg.subject}\n{msg.text[:3000]}"
            if msg.ref in used or not ACK.search(text) or not _mentions(msg, app["name"]):
                continue
            used.add(msg.ref)
            with conn.tx():
                _record(conn, provider, msg, "acknowledgement", app["id"], None)
                ev = {"kind": "acknowledgement e-mail", "message_ref": msg.ref, "received_at": msg.received_at,
                      "sender_domain": msg.sender_domain, "title_named": normalise(app["role_title"] or "") in normalise(text)}
                if app["state"] == "uncertain":
                    submission.resolve_uncertain(conn, app["id"], True, ev, actor="mail-reconciler")
                else:
                    conn.event("application.acknowledged", "application", app["id"], ev)
            done.append(app["id"])
            break
    return done
