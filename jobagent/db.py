"""Thin portable database layer: SQLite for local use, PostgreSQL for shared use.

SQL is written once with `?` placeholders and only features both engines support
(ON CONFLICT, RETURNING, partial unique indexes). Rows come back as dicts.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
if not MIGRATIONS.exists():  # installed layout
    MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def unj(text, default=None):
    if text is None or text == "":
        return default
    return json.loads(text)


class Conn:
    """One connection. Autocommit outside `tx()`; `tx()` blocks are atomic."""

    def __init__(self, url: str):
        self.url = url
        self.is_pg = url.startswith(("postgres://", "postgresql://"))
        self._depth = 0
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row

            self.raw = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        else:
            path = url.removeprefix("sqlite:///")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.raw = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
            self.raw.row_factory = sqlite3.Row
            self.raw.execute("PRAGMA journal_mode=WAL")
            self.raw.execute("PRAGMA foreign_keys=ON")
            self.raw.execute("PRAGMA busy_timeout=30000")

    def q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        if self.is_pg:
            cur = self.raw.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
            return list(cur.fetchall()) if cur.description else []
        cur = self.raw.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()] if cur.description else []

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def val(self, sql: str, params: tuple | list = ()):
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    @contextmanager
    def tx(self, lock: str | None = None):
        """Atomic block. `lock` names a cross-worker critical section.

        SQLite: BEGIN IMMEDIATE takes the single write lock. PostgreSQL: a
        transaction-scoped advisory lock keyed by the name. Nested calls join
        the outer transaction.
        """
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.raw.execute("BEGIN" if self.is_pg else "BEGIN IMMEDIATE")
        self._depth = 1
        try:
            if lock and self.is_pg:
                self.raw.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
            yield self
            self.raw.execute("COMMIT")
        except BaseException:
            self.raw.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    def close(self):
        self.raw.close()

    # -- audit log -----------------------------------------------------------
    def event(self, kind: str, entity_type: str | None = None, entity_id: str | None = None,
              data: dict | None = None, actor: str = "system", provenance: str | None = None):
        self.q("INSERT INTO events (id, ts, actor, kind, entity_type, entity_id, data, provenance) VALUES (?,?,?,?,?,?,?,?)",
               (new_id("ev"), now(), actor, kind, entity_type, entity_id, j(data or {}), provenance))


def connect(url: str | None = None) -> Conn:
    return Conn(url or config.database_url())


def migrate(conn: Conn) -> list[str]:
    conn.q("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r["name"] for r in conn.q("SELECT name FROM schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with conn.tx(lock="migrate"):
            if conn.one("SELECT 1 FROM schema_migrations WHERE name=?", (path.name,)):
                continue
            for stmt in _split(path.read_text()):
                conn.raw.execute(stmt)
            conn.q("INSERT INTO schema_migrations (name, applied_at) VALUES (?,?)", (path.name, now()))
        applied.append(path.name)
    return applied


def _split(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


# -- settings ------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "discovery_enabled": True,
    "preparation_enabled": True,
    "submission_enabled": False,   # live submission is off until onboarding is complete
    "global_pause": False,
    "dry_run": True,
}


def get_setting(conn: Conn, key: str):
    row = conn.one("SELECT value FROM settings WHERE key=?", (key,))
    return unj(row["value"]) if row else DEFAULT_SETTINGS.get(key)


def set_setting(conn: Conn, key: str, value, actor: str = "user"):
    conn.q("INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
           "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, j(value), now()))
    conn.event("setting.changed", "setting", key, {"value": value}, actor=actor)


def all_settings(conn: Conn) -> dict:
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: unj(r["value"]) for r in conn.q("SELECT key, value FROM settings")})
    return out
