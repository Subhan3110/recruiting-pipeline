"""Recruitment portal accounts: metadata here, credentials only as references into the secret store.

Scope is (portal_origin, tenant, login_identifier). Portals such as Workday keep
separate accounts per employer tenant, so the tenant is part of the identity.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import policy as policy_mod, profile, secretstore
from .db import Conn, new_id, now


def origin_of(url: str) -> str:
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError(f"portal URL must be https: {url!r}")
    return f"https://{u.hostname.lower()}" + (f":{u.port}" if u.port and u.port != 443 else "")


def find(conn: Conn, portal_origin: str, tenant: str, login: str | None = None) -> dict | None:
    sql = "SELECT * FROM accounts WHERE portal_origin=? AND tenant=? AND status<>'deleted'"
    params = [portal_origin, tenant or ""]
    if login:
        sql += " AND login_identifier=?"
        params.append(login.lower())
    return conn.one(sql + " ORDER BY created_at LIMIT 1", params)


def plan(conn: Conn, portal_origin: str, tenant: str, login: str) -> dict:
    """Decide the next account step without touching the portal.

    -> {'action': 'login'|'check_then_register'|'blocked', 'account': row|None, 'reason': str}
    """
    _, cfg = policy_mod.current(conn)
    acct = find(conn, portal_origin, tenant, login)
    if acct:
        if acct["status"] == "locked":
            return {"action": "blocked", "account": acct, "reason": "earlier logins failed; waiting for credential recovery"}
        if acct["pending_action"]:
            return {"action": "blocked", "account": acct, "reason": f"pending: {acct['pending_action']}"}
        return {"action": "login", "account": acct, "reason": "known account"}
    if not cfg["accounts"]["create_accounts"]:
        return {"action": "blocked", "account": None, "reason": "no account on file and account creation is disabled in the policy"}
    # The portal's own flow ("email already registered?") must be checked before registering.
    return {"action": "check_then_register", "account": None, "reason": "no account on file for this portal and tenant"}


def begin_registration(conn: Conn, portal_origin: str, tenant: str, login: str, employer_id: str | None = None,
                       password_policy: dict | None = None, supplied_ref: str | None = None) -> tuple[dict, str]:
    """Persist the account and its credential *before* the portal sees them, so a crash cannot orphan an account.

    Uses a user-supplied recruitment credential when one is configured for this scope, otherwise a unique generated password.
    Mailbox credentials are never eligible: secretstore enforces the 'recruitment' scope.
    """
    with conn.tx(lock="accounts"):
        existing = find(conn, portal_origin, tenant, login)
        if existing:
            return existing, secretstore.get(conn, existing["credential_ref"], "recruitment")
        aid = new_id("acct")
        if supplied_ref:
            password, ref, source = secretstore.get(conn, supplied_ref, "recruitment"), supplied_ref, "user-supplied"
        else:
            password, ref, source = secretstore.generate_password(policy=password_policy), f"account/{aid}", "generated"
            secretstore.put(conn, ref, password, "recruitment")
        ts = now()
        conn.q("INSERT INTO accounts (id, portal_origin, tenant, employer_id, login_identifier, credential_ref, credential_source, "
               "verification_status, pending_action, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (aid, portal_origin, tenant or "", employer_id, login.lower(), ref, source, "unverified", "registration in progress",
                "pending", ts, ts))
        conn.event("account.registration_started", "account", aid, {"portal": portal_origin, "tenant": tenant, "credential_source": source})
        return conn.one("SELECT * FROM accounts WHERE id=?", (aid,)), password


def replace_password(conn: Conn, account_id: str, password_policy: dict) -> str:
    """The portal rejected the password (policy mismatch): generate a compliant one under a new reference.

    The old reference is kept until a login with the new one succeeds, so access is never silently lost.
    """
    acct = conn.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    n = conn.val("SELECT COUNT(*) FROM secrets WHERE ref LIKE ?", (f"account/{account_id}%",))
    ref = f"account/{account_id}/v{n + 1}"
    pw = secretstore.generate_password(policy=password_policy)
    secretstore.put(conn, ref, pw, "recruitment")
    conn.q("UPDATE accounts SET pending_action=?, updated_at=? WHERE id=?", (f"confirm new credential {ref} (previous: {acct['credential_ref']})", now(), account_id))
    return pw


def record_login(conn: Conn, account_id: str, ok: bool, credential_ref: str | None = None):
    """Record a login result. Repeated failures lock the account and raise a recovery task instead of retrying."""
    _, cfg = policy_mod.current(conn)
    acct = conn.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if ok:
        old = acct["credential_ref"]
        conn.q("UPDATE accounts SET last_login_ok=?, failed_logins=0, status='active', pending_action=NULL, credential_ref=?, updated_at=? WHERE id=?",
               (now(), credential_ref or old, now(), account_id))
        if credential_ref and old and credential_ref != old and acct["credential_source"] == "generated":
            secretstore.delete(conn, old)  # superseded generated password; the working one is confirmed
        conn.event("account.login_ok", "account", account_id)
        return
    fails = acct["failed_logins"] + 1
    locked = fails >= cfg["accounts"]["max_failed_logins"]
    conn.q("UPDATE accounts SET failed_logins=?, status=?, updated_at=? WHERE id=?", (fails, "locked" if locked else acct["status"], now(), account_id))
    conn.event("account.login_failed", "account", account_id, {"failures": fails})
    if locked:
        recovery = "Password recovery is enabled in the policy; the agent may use it." if cfg["accounts"]["password_recovery"] else \
            "Password recovery is not authorised in the policy. Supply working credentials or enable recovery."
        profile.open_task(conn, "recovery", f"Login failed for {acct['login_identifier']} at {acct['portal_origin']}"
                          + (f" ({acct['tenant']})" if acct["tenant"] else ""),
                          f"The account exists but the stored credentials were rejected {fails} times. No further attempts will be made. {recovery}",
                          "account", account_id, dedup_key=f"acct-recovery:{account_id}")


def mark_verified(conn: Conn, account_id: str, message_ref: str | None = None):
    conn.q("UPDATE accounts SET verification_status='verified', pending_action=NULL, status='active', updated_at=? WHERE id=?", (now(), account_id))
    conn.event("account.verified", "account", account_id, {"message_ref": message_ref})


def set_pending(conn: Conn, account_id: str, action: str):
    """Park the account on an interactive requirement (CAPTCHA, authenticator, phone) without losing progress."""
    conn.q("UPDATE accounts SET pending_action=?, updated_at=? WHERE id=?", (action, now(), account_id))
    acct = conn.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    profile.open_task(conn, "manual_action", f"{action}: {acct['portal_origin']}", "The application will continue once this is done.",
                      "account", account_id, dedup_key=f"acct-pending:{account_id}")


def listing(conn: Conn) -> list[dict]:
    """Metadata only. Passwords are never selected."""
    return conn.q("SELECT a.id, a.portal_origin, a.tenant, a.login_identifier, a.credential_source, a.verification_status, a.last_login_ok, "
                  "a.failed_logins, a.pending_action, a.status, a.created_at, e.name AS employer_name, "
                  "CASE WHEN a.credential_ref IS NULL THEN 0 ELSE 1 END AS has_credential "
                  "FROM accounts a LEFT JOIN employers e ON e.id=a.employer_id WHERE a.status<>'deleted' ORDER BY a.created_at")
