"""Mailbox via a relay table, filled by a Claude session that holds a mail connector (e.g. the claude.ai Gmail connector).

The worker cannot call connectors itself, so a Claude session searches the mailbox for recruitment mail and
hands the matching messages to `python -m jobagent mail-relay-add` (see docs/MAIL_RELAY.md). This provider then
reads them like any other mailbox. Relayed text is untrusted input and is only ever parsed for codes, links and
acknowledgements by jobagent.verification.
"""
from __future__ import annotations

import json
import re

from ..db import Conn, new_id, now, unj
from .base import MailMessage, MailProvider

MAX_TEXT = 6000


class RelayMail(MailProvider):
    name = "relay"

    def __init__(self, conn: Conn):
        self.conn = conn

    def search(self, since_iso, recipient=None, sender_domains=()):
        out = []
        for r in self.conn.q("SELECT * FROM mail_relay WHERE received_at >= ? ORDER BY received_at", (since_iso,)):
            m = MailMessage(r["message_ref"], r["received_at"], r["sender"], unj(r["recipients"], []), r["subject"] or "", r["text"] or "")
            if recipient and recipient.lower() not in [x.lower() for x in m.recipients]:
                continue
            if sender_domains and not any(m.sender_domain == d or m.sender_domain.endswith("." + d) for d in sender_domains):
                continue
            out.append(m)
        return out

    def check(self):
        n = self.conn.val("SELECT COUNT(*) FROM mail_relay")
        last = self.conn.val("SELECT MAX(created_at) FROM mail_relay")
        return f"relay mailbox: {n} relayed messages, last relay {last or 'never'}"


_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[a-z]{2,}", re.I)


def add(conn: Conn, messages: list[dict], provider: str = "gmail_connector") -> dict:
    """Store relayed messages. Each: {ref, received_at (ISO), sender, recipients [..], subject, text}. Duplicates are ignored."""
    added = skipped = 0
    for m in messages:
        sender = (_EMAIL.search(str(m.get("sender", ""))) or [None])[0]
        recips = [x.group(0).lower() for r in m.get("recipients", []) for x in [_EMAIL.search(str(r))] if x]
        if not (m.get("ref") and m.get("received_at") and sender and recips):
            skipped += 1
            continue
        rows = conn.q("INSERT INTO mail_relay (id, provider, message_ref, received_at, sender, recipients, subject, text, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (provider, message_ref) DO NOTHING RETURNING id",
                      (new_id("mr"), provider, str(m["ref"]), str(m["received_at"]), sender.lower(), json.dumps(recips),
                       str(m.get("subject", ""))[:300], str(m.get("text", ""))[:MAX_TEXT], now()))
        added += len(rows)
    conn.event("mail.relayed", data={"added": added, "skipped": skipped, "provider": provider})
    return {"added": added, "skipped": skipped}


def needs(conn: Conn) -> dict:
    """What the relaying session should look for: pending verification codes and applications awaiting an acknowledgement."""
    pending = conn.q("SELECT id, purpose, portal_origin, employer_hint, recipient, requested_at, expires_at FROM verification_requests "
                     "WHERE status='pending' ORDER BY requested_at")
    awaiting = conn.q("SELECT a.id, e.name AS employer, a.role_title, a.state, MIN(t.submit_clicked_at) AS clicked FROM applications a "
                      "JOIN employers e ON e.id=a.employer_id JOIN attempts t ON t.application_id=a.id "
                      "WHERE a.state IN ('uncertain','submitted') AND t.submit_clicked_at IS NOT NULL "
                      "AND NOT EXISTS (SELECT 1 FROM email_messages m WHERE m.application_id=a.id AND m.kind='acknowledgement') "
                      "GROUP BY a.id, e.name, a.role_title, a.state ORDER BY clicked")
    since = min([p["requested_at"] for p in pending] + [a["clicked"] for a in awaiting], default=None)
    return {"verification_codes": pending, "acknowledgements": awaiting, "search_since": since,
            "suggested_gmail_query": "from:(greenhouse-mail.io OR greenhouse.io) " + (f"after:{since[:10].replace('-', '/')}" if since else "newer_than:2d")}
