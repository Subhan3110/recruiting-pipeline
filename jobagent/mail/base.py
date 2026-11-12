"""Mailbox access. Providers are read-only: the agent never sends, deletes or labels mail.

E-mail content is untrusted input. Providers return plain data; only
jobagent.verification decides what, if anything, is done with it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class MailMessage:
    ref: str            # provider message id (stable reference kept as evidence)
    received_at: str    # ISO-8601 UTC
    sender: str         # address only
    recipients: list[str]
    subject: str
    text: str
    html: str = ""

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower() if "@" in self.sender else ""


class MailProvider:
    name = "base"

    def search(self, since_iso: str, recipient: str | None = None, sender_domains: tuple[str, ...] = ()) -> list[MailMessage]:
        """Messages received at or after `since_iso`, optionally narrowed to a recipient and sender domains."""
        raise NotImplementedError

    def check(self) -> str:
        """Connectivity check that returns a short status and never any mailbox content."""
        raise NotImplementedError


class FixtureMail(MailProvider):
    """In-memory mailbox for tests and dry runs."""
    name = "fixture"

    def __init__(self, messages: list[MailMessage] | None = None):
        self.messages = messages or []

    def search(self, since_iso, recipient=None, sender_domains=()):
        out = []
        for m in self.messages:
            if m.received_at < since_iso:
                continue
            if recipient and recipient.lower() not in [r.lower() for r in m.recipients]:
                continue
            if sender_domains and not any(m.sender_domain == d or m.sender_domain.endswith("." + d) for d in sender_domains):
                continue
            out.append(m)
        return sorted(out, key=lambda m: m.received_at)

    def check(self):
        return "fixture mailbox"


def get_provider(conn) -> MailProvider | None:
    """Provider chosen by the 'mail_provider' setting: 'gmail_api' (read-only OAuth scope) or 'imap'."""
    from .. import db

    kind = db.get_setting(conn, "mail_provider")
    if kind == "gmail_api":
        from .gmail_api import GmailApi

        return GmailApi(conn)
    if kind == "imap":
        from .imap import ImapMail

        return ImapMail(conn)
    return None
