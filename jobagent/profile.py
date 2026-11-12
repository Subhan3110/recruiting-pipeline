"""Durable candidate memory: facts with provenance and history, verbatim comments, and user tasks."""
from __future__ import annotations

from .db import Conn, j, new_id, now, unj

STATUSES = ("confirmed", "unconfirmed", "preference", "proposed")


def set_fact(conn: Conn, key: str, value, status: str, source: str, note: str | None = None, actor: str = "user") -> str:
    """Record a fact. An existing current fact for the key is superseded, never deleted."""
    assert status in STATUSES, status
    with conn.tx():
        ts = now()
        fid = new_id("fact")
        old = conn.one("SELECT id, value, status FROM profile_facts WHERE key=? AND superseded_at IS NULL", (key,))
        if old:
            if old["value"] == j(value) and old["status"] == status:
                return old["id"]
            conn.q("UPDATE profile_facts SET superseded_at=?, superseded_by=? WHERE id=?", (ts, fid, old["id"]))
        confirmed = ts if status in ("confirmed", "preference") else None
        conn.q("INSERT INTO profile_facts (id, key, value, status, source, confirmed_at, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (fid, key, j(value), status, source, confirmed, note, ts))
        conn.event("profile.fact_set", "fact", key, {"status": status, "superseded": old["id"] if old else None},
                   actor=actor, provenance=source)
        return fid


def confirm_fact(conn: Conn, key: str, actor: str = "user") -> bool:
    row = conn.one("SELECT value, source, note FROM profile_facts WHERE key=? AND superseded_at IS NULL", (key,))
    if not row:
        return False
    set_fact(conn, key, unj(row["value"]), "confirmed", f"{row['source']}; confirmed by {actor}", row["note"], actor)
    return True


def retire_fact(conn: Conn, key: str, actor: str = "user"):
    conn.q("UPDATE profile_facts SET superseded_at=? WHERE key=? AND superseded_at IS NULL", (now(), key))
    conn.event("profile.fact_retired", "fact", key, actor=actor)


def get_fact(conn: Conn, key: str, confirmed_only: bool = False):
    row = conn.one("SELECT value, status FROM profile_facts WHERE key=? AND superseded_at IS NULL", (key,))
    if not row or (confirmed_only and row["status"] not in ("confirmed", "preference")):
        return None
    return unj(row["value"])


def facts(conn: Conn, prefix: str = "") -> list[dict]:
    rows = conn.q("SELECT * FROM profile_facts WHERE superseded_at IS NULL AND key LIKE ? ORDER BY key", (prefix + "%",))
    for r in rows:
        r["value"] = unj(r["value"])
    return rows


def history(conn: Conn, key: str) -> list[dict]:
    return conn.q("SELECT * FROM profile_facts WHERE key=? ORDER BY created_at", (key,))


def snapshot(conn: Conn) -> dict:
    """Current facts as {key: {value, status, source}}; stored with every prepared application."""
    return {r["key"]: {"value": r["value"], "status": r["status"], "source": r["source"]} for r in facts(conn)}


def add_comment(conn: Conn, verbatim: str, source: str, interpretation: dict | None = None) -> str:
    cid = new_id("cmt")
    conn.q("INSERT INTO profile_comments (id, ts, source, verbatim, interpretation) VALUES (?,?,?,?,?)",
           (cid, now(), source, verbatim, j(interpretation) if interpretation else None))
    conn.event("profile.comment_added", "comment", cid, provenance=source)
    return cid


def comments(conn: Conn) -> list[dict]:
    return conn.q("SELECT * FROM profile_comments ORDER BY ts")


# -- user tasks (questions, manual actions, recovery) ---------------------------
def open_task(conn: Conn, kind: str, title: str, detail: str = "", entity_type: str | None = None,
              entity_id: str | None = None, dedup_key: str | None = None) -> str | None:
    """Create a task for the user once; a still-open task with the same dedup_key is reused."""
    if dedup_key:
        row = conn.one("SELECT id, status FROM user_tasks WHERE dedup_key=?", (dedup_key,))
        if row:
            if row["status"] != "open":
                conn.q("UPDATE user_tasks SET status='open', resolution=NULL, resolved_at=NULL, detail=? WHERE id=?", (detail, row["id"]))
            return row["id"]
    tid = new_id("task")
    conn.q("INSERT INTO user_tasks (id, created_at, kind, title, detail, entity_type, entity_id, dedup_key) VALUES (?,?,?,?,?,?,?,?)",
           (tid, now(), kind, title, detail, entity_type, entity_id, dedup_key))
    conn.event("task.opened", "task", tid, {"kind": kind, "title": title})
    return tid


def resolve_task(conn: Conn, task_id: str, resolution: str = "", status: str = "done", actor: str = "user"):
    conn.q("UPDATE user_tasks SET status=?, resolution=?, resolved_at=? WHERE id=?", (status, resolution, now(), task_id))
    conn.event("task.resolved", "task", task_id, {"status": status}, actor=actor)


def resolve_tasks_by_key(conn: Conn, dedup_key: str, resolution: str = ""):
    row = conn.one("SELECT id FROM user_tasks WHERE dedup_key=? AND status='open'", (dedup_key,))
    if row:
        resolve_task(conn, row["id"], resolution, actor="system")


def open_tasks(conn: Conn) -> list[dict]:
    return conn.q("SELECT * FROM user_tasks WHERE status='open' ORDER BY created_at")


def import_profile(conn: Conn, data: dict, actor: str = "import") -> dict:
    """Load a profile seed: {'facts': [...], 'comments': [...], 'questions': [...], 'answers': [...]}"""
    from . import answers as answers_mod

    n = {"facts": 0, "comments": 0, "questions": 0, "answers": 0}
    for f in data.get("facts", []):
        set_fact(conn, f["key"], f["value"], f.get("status", "unconfirmed"), f["source"], f.get("note"), actor)
        n["facts"] += 1
    existing = {c["verbatim"] for c in comments(conn)}
    for c in data.get("comments", []):
        if c["verbatim"] not in existing:
            add_comment(conn, c["verbatim"], c["source"], c.get("interpretation"))
            n["comments"] += 1
    for q in data.get("questions", []):
        open_task(conn, "question", q["title"], q.get("detail", ""), dedup_key="q:" + q["key"])
        n["questions"] += 1
    for a in data.get("answers", []):
        answers_mod.set_answer(conn, a["question_key"], a["answer"], a.get("scope_type", "global"), a.get("scope_value", ""),
                               a.get("status", "proposed"), a["source"])
        n["answers"] += 1
    return n
