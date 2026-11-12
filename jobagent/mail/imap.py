"""Generic IMAP mailbox (any provider). Opens the folder read-only; never flags or deletes.

An IMAP app password grants broader access than the Gmail read-only OAuth scope, so
prefer gmail_api for Gmail and use IMAP for a dedicated job-application mailbox elsewhere.
Settings: mail_imap_host, mail_imap_user, mail_imap_folder. Secret (scope 'mailbox'): mail/imap/password.
NOT YET VERIFIED LIVE.
"""
from __future__ import annotations

import email
import imaplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from .. import db, secretstore
from .base import MailMessage, MailProvider


class ImapMail(MailProvider):
    name = "imap"

    def __init__(self, conn):
        self.conn = conn

    def _open(self):
        host, user = db.get_setting(self.conn, "mail_imap_host"), db.get_setting(self.conn, "mail_imap_user")
        box = imaplib.IMAP4_SSL(host)
        box.login(user, secretstore.get(self.conn, "mail/imap/password", "mailbox"))
        box.select(db.get_setting(self.conn, "mail_imap_folder") or "INBOX", readonly=True)
        return box

    def search(self, since_iso, recipient=None, sender_domains=()):
        since = datetime.fromisoformat(since_iso)
        box = self._open()
        try:
            crit = ["SINCE", since.strftime("%d-%b-%Y")] + (["TO", f'"{recipient}"'] if recipient else [])
            _, data = box.uid("search", None, *crit)
            out = []
            for uid in data[0].split()[-50:]:
                _, parts = box.uid("fetch", uid, "(BODY.PEEK[])")
                msg = email.message_from_bytes(parts[0][1])
                when = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc)
                sender = parseaddr(msg.get("From", ""))[1].lower()
                dom = sender.rsplit("@", 1)[-1]
                if when < since or (sender_domains and not any(dom == d or dom.endswith("." + d) for d in sender_domains)):
                    continue
                text = html = ""
                for p in msg.walk():
                    if p.get_content_maintype() == "text" and not p.get_filename():
                        body = (p.get_payload(decode=True) or b"").decode(p.get_content_charset() or "utf-8", "replace")
                        if p.get_content_subtype() == "plain":
                            text += body
                        elif p.get_content_subtype() == "html":
                            html += body
                out.append(MailMessage(ref=f"imap:{msg.get('Message-ID', uid.decode())}", received_at=when.isoformat(timespec="milliseconds"),
                                       sender=sender, recipients=[a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Delivered-To", [])) if a],
                                       subject=str(make_header(decode_header(msg.get("Subject", "")))), text=text, html=html))
            return sorted(out, key=lambda m: m.received_at)
        finally:
            box.logout()

    def check(self):
        box = self._open()
        try:
            return "imap login ok"
        finally:
            box.logout()
