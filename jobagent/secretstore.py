"""Secret manager: values are Fernet-encrypted with JOBAGENT_MASTER_KEY and stored in the shared database.

Only the master key lives in each runtime's environment secrets, so Claude Code,
Codex and the worker share credentials without any of them printing a value.
Rows carry a scope ('mailbox', 'recruitment', 'api'); callers must name the scope
they are entitled to, which keeps mailbox credentials out of account creation.
"""
from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
import string

from cryptography.fernet import Fernet, InvalidToken

from . import config
from .db import Conn, now

SCOPES = {"mailbox", "recruitment", "api"}


class SecretError(RuntimeError):
    pass


def generate_master_key() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = config.master_key()
    if not key:
        raise SecretError("JOBAGENT_MASTER_KEY is not set; secrets and private documents are unavailable")
    try:
        return Fernet(key.encode())
    except Exception as e:  # noqa: BLE001
        raise SecretError("JOBAGENT_MASTER_KEY is not a valid Fernet key") from e


def encrypt(data: bytes) -> str:
    return _fernet().encrypt(data).decode()


def decrypt(token: str) -> bytes:
    try:
        return _fernet().decrypt(token.encode())
    except InvalidToken as e:
        raise SecretError("stored value cannot be decrypted with the configured master key") from e


def put(conn: Conn, ref: str, value: str, scope: str):
    if scope not in SCOPES:
        raise SecretError(f"unknown secret scope {scope!r}")
    ts = now()
    conn.q("INSERT INTO secrets (ref, scope, ciphertext, created_at, updated_at) VALUES (?,?,?,?,?) "
           "ON CONFLICT (ref) DO UPDATE SET ciphertext=excluded.ciphertext, scope=excluded.scope, updated_at=excluded.updated_at",
           (ref, scope, encrypt(value.encode()), ts, ts))
    conn.event("secret.stored", "secret", ref, {"scope": scope})  # never the value


def get(conn: Conn, ref: str, scope: str) -> str:
    row = conn.one("SELECT scope, ciphertext FROM secrets WHERE ref=?", (ref,))
    if not row:
        raise SecretError(f"no secret stored under {ref!r}")
    if row["scope"] != scope:
        raise SecretError(f"secret {ref!r} belongs to scope {row['scope']!r}, not {scope!r}")
    return decrypt(row["ciphertext"]).decode()


def exists(conn: Conn, ref: str) -> bool:
    return conn.one("SELECT 1 FROM secrets WHERE ref=?", (ref,)) is not None


def delete(conn: Conn, ref: str):
    conn.q("DELETE FROM secrets WHERE ref=?", (ref,))
    conn.event("secret.deleted", "secret", ref)


def listing(conn: Conn) -> list[dict]:
    return conn.q("SELECT ref, scope, created_at, updated_at FROM secrets ORDER BY ref")


def fingerprint(value: str) -> str:
    """Short non-reversible tag for comparing values without exposing them."""
    return base64.b32encode(hashlib.sha256(value.encode()).digest()[:5]).decode().lower()


def generate_password(length: int = 24, policy: dict | None = None) -> str:
    """Random password meeting a portal policy: {'max_length','min_length','symbols','no_symbols'}."""
    policy = policy or {}
    length = max(policy.get("min_length", 12), min(length, policy.get("max_length", length)))
    symbols = "" if policy.get("no_symbols") else policy.get("symbols", "!@#$%*-_+")
    classes = [string.ascii_lowercase, string.ascii_uppercase, string.digits] + ([symbols] if symbols else [])
    alphabet = "".join(classes)
    while True:
        pw = "".join(pysecrets.choice(alphabet) for _ in range(length))
        if all(any(c in cls for c in pw) for cls in classes):
            return pw
