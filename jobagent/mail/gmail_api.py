"""Gmail via the Gmail REST API with the read-only scope (https://www.googleapis.com/auth/gmail.readonly).

That scope is the narrowest Gmail offers for reading message bodies; it cannot
send, delete or modify mail. Google has no per-label or per-sender scope, which
is why a dedicated job-application mailbox or alias is recommended.
Secrets (scope 'mailbox'): mail/gmail/client_id, mail/gmail/client_secret, mail/gmail/refresh_token.
NOT YET VERIFIED LIVE: needs gmail.googleapis.com and oauth2.googleapis.com on the network allowlist.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr

import requests

from .. import secretstore
from .base import MailMessage, MailProvider

API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailApi(MailProvider):
    name = "gmail_api"

    def __init__(self, conn):
        self.conn = conn
        self._token = None

    def _access_token(self) -> str:
        if not self._token:
            g = lambda k: secretstore.get(self.conn, f"mail/gmail/{k}", "mailbox")  # noqa: E731
            r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
                "client_id": g("client_id"), "client_secret": g("client_secret"), "refresh_token": g("refresh_token"),
                "grant_type": "refresh_token"})
            if r.status_code != 200:
                raise RuntimeError(f"Gmail token refresh failed with HTTP {r.status_code}")  # body may contain secrets: not logged
            self._token = r.json()["access_token"]
        return self._token

    def _get(self, path: str, **params):
        r = requests.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {self._access_token()}"}, timeout=30)
        r.raise_for_status()
        return r.json()

    def search(self, since_iso, recipient=None, sender_domains=()):
        since = int(datetime.fromisoformat(since_iso).timestamp())
        q = [f"after:{since}"]
        if recipient:
            q.append(f"to:{recipient}")
        if sender_domains:
            q.append("{" + " ".join(f"from:{d}" for d in sender_domains) + "}")
        out = []
        for item in self._get("messages", q=" ".join(q), maxResults=25).get("messages", []):
            msg = self._get(f"messages/{item['id']}", format="full")
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            text, html = _bodies(msg["payload"])
            out.append(MailMessage(
                ref=f"gmail:{msg['id']}",
                received_at=datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc).isoformat(timespec="milliseconds"),
                sender=parseaddr(headers.get("from", ""))[1].lower(),
                recipients=[a.lower() for _, a in getaddresses([headers.get("to", ""), headers.get("delivered-to", "")]) if a],
                subject=headers.get("subject", ""), text=text, html=html))
        return sorted(out, key=lambda m: m.received_at)

    def check(self):
        return f"gmail profile reachable, {self._get('profile').get('messagesTotal', '?')} messages"


def _bodies(part: dict) -> tuple[str, str]:
    text = html = ""
    stack = [part]
    while stack:
        p = stack.pop()
        stack.extend(p.get("parts", []))
        data = p.get("body", {}).get("data")
        if not data:
            continue
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        if p.get("mimeType") == "text/plain":
            text += decoded
        elif p.get("mimeType") == "text/html":
            html += decoded
    return text, html
