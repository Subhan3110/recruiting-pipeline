"""Durable job queue in the database: survives restarts, safe with several workers, retries with backoff."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Conn, ago, j, new_id, now, unj


def enqueue(conn: Conn, kind: str, payload: dict | None = None, dedup_key: str | None = None, delay_seconds: int = 0,
            max_attempts: int = 5) -> str | None:
    run_after = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(timespec="milliseconds")
    jid = new_id("job")
    rows = conn.q("INSERT INTO jobs (id, kind, payload, dedup_key, run_after, max_attempts, created_at) VALUES (?,?,?,?,?,?,?) "
                  "ON CONFLICT DO NOTHING RETURNING id", (jid, kind, j(payload or {}), dedup_key, run_after, max_attempts, now()))
    return rows[0]["id"] if rows else None


def claim(conn: Conn, worker: str) -> dict | None:
    rows = conn.q("UPDATE jobs SET status='running', locked_by=?, locked_at=?, attempts=attempts+1 "
                  "WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=? ORDER BY run_after LIMIT 1) AND status='queued' RETURNING *",
                  (worker, now(), now()))
    if rows:
        rows[0]["payload"] = unj(rows[0]["payload"], {})
        return rows[0]
    return None


def complete(conn: Conn, job_id: str):
    conn.q("UPDATE jobs SET status='done', finished_at=?, locked_by=NULL WHERE id=?", (now(), job_id))


def fail(conn: Conn, job: dict, error: str):
    """Retry with exponential backoff; give up after max_attempts."""
    if job["attempts"] >= job["max_attempts"]:
        conn.q("UPDATE jobs SET status='failed', finished_at=?, last_error=?, locked_by=NULL WHERE id=?", (now(), error[:500], job["id"]))
        return
    delay = min(3600, 30 * 2 ** job["attempts"])
    run_after = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
    conn.q("UPDATE jobs SET status='queued', run_after=?, last_error=?, locked_by=NULL WHERE id=?", (run_after, error[:500], job["id"]))


def recover(conn: Conn, stale_minutes: int = 30, worker: str | None = None) -> int:
    """Jobs left 'running' by a dead worker go back to the queue."""
    rows = conn.q("UPDATE jobs SET status='queued', locked_by=NULL, last_error='worker stopped mid-job' "
                  "WHERE status='running' AND (locked_at < ? OR locked_by = ?) RETURNING id", (ago(minutes=stale_minutes), worker or ""))
    return len(rows)
