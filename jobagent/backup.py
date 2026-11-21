"""Backups, restoration, retention and deletion. Dumps are engine-neutral JSON; secrets stay encrypted inside them."""
from __future__ import annotations

import gzip
import json

from . import db as dbmod
from .db import Conn, ago, now

TABLES = ["schema_migrations", "events", "settings", "policy_versions", "profile_facts", "profile_comments", "answers", "user_tasks",
          "employers", "employer_aliases", "vacancies", "vacancy_sources", "evaluations", "documents", "secrets", "accounts",
          "applications", "application_versions", "approvals", "transitions", "attempts", "employer_slots", "verification_requests",
          "email_messages", "jobs", "model_usage", "mail_relay"]


PORTABLE_SKIP = ("documents", "secrets")  # ciphertext is tied to one master key; rebuilt or re-entered on the new database


def dump(conn: Conn, path: str, portable: bool = False) -> dict:
    """Engine-neutral dump. portable=True leaves out encrypted documents and secrets so the dump can move to a database with a new master key."""
    counts = {}
    with conn.tx(), gzip.open(path, "wt", encoding="utf-8") as fh:
        for t in TABLES:
            if portable and t in PORTABLE_SKIP:
                continue
            rows = conn.q(f"SELECT * FROM {t}")
            counts[t] = len(rows)
            fh.write(json.dumps({"table": t, "rows": rows}, default=str) + "\n")
    conn.event("backup.created", "backup", path.rsplit("/", 1)[-1], {"rows": sum(counts.values())})
    return counts


def restore(conn: Conn, path: str) -> dict:
    """Restore into a freshly migrated, empty database. Refuses to overwrite existing operational data."""
    dbmod.migrate(conn)
    if conn.val("SELECT COUNT(*) FROM applications") or conn.val("SELECT COUNT(*) FROM profile_facts"):
        raise RuntimeError("target database is not empty; restore only into a new database")
    counts = {}
    with conn.tx(), gzip.open(path, "rt", encoding="utf-8") as fh:
        conn.q("DELETE FROM policy_versions")
        conn.q("DELETE FROM settings")
        conn.q("DELETE FROM events")
        for line in fh:
            rec = json.loads(line)
            t = rec["table"]
            if t == "schema_migrations" or t not in TABLES:
                continue
            rows = rec["rows"]
            if rows:
                cols = list(rows[0])
                conn.many(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [[r[c] for c in cols] for r in rows])
            counts[t] = len(rows)
        if "documents" not in counts:
            # Documents were not carried over: send unfinished applications back to preparation so the PDFs are rebuilt under this database's key.
            moved = conn.q("UPDATE applications SET state='evaluated', state_reason='documents rebuilt after moving to a new database', updated_at=? "
                           "WHERE origin='agent' AND state IN ('prepared','awaiting_approval','queued','needs_input') RETURNING id", (now(),))
            conn.q("DELETE FROM employer_slots WHERE application_id IN (SELECT id FROM applications WHERE state='evaluated' AND origin='agent')")
            conn.q("DELETE FROM approvals")  # approvals were bound to document hashes that no longer exist
            counts["applications_sent_back_to_preparation"] = len(moved)
    return counts


def apply_retention(conn: Conn, cfg: dict) -> dict:
    """Drop raw source payloads and e-mail evidence past their retention period. Application history is kept."""
    r = cfg["retention"]
    a = conn.q("UPDATE vacancy_sources SET raw=NULL WHERE last_seen < ? AND raw IS NOT NULL RETURNING id", (ago(days=r["vacancy_raw_days"]),))
    b = conn.q("DELETE FROM email_messages WHERE created_at < ? RETURNING id", (ago(days=r["email_evidence_days"]),))
    conn.q("DELETE FROM mail_relay WHERE created_at < ?", (ago(days=14),))
    c = conn.q("DELETE FROM jobs WHERE status IN ('done','failed') AND finished_at < ? RETURNING id", (ago(days=30),))
    return {"vacancy_raw_cleared": len(a), "email_evidence_deleted": len(b), "jobs_deleted": len(c)}


def delete_personal_data(conn: Conn, confirm: str) -> dict:
    """Erase the candidate profile, documents, answers, credentials and mail evidence. Irreversible."""
    if confirm != "DELETE MY DATA":
        raise ValueError("confirmation phrase required")
    out = {}
    with conn.tx(lock="submission"):
        dbmod.set_setting(conn, "global_pause", True, actor="system")
        for t in ("profile_facts", "profile_comments", "answers", "secrets", "email_messages", "verification_requests", "documents", "mail_relay",
                  "application_versions"):
            out[t] = len(conn.q(f"DELETE FROM {t} RETURNING 1 AS x"))
        conn.q("UPDATE accounts SET credential_ref=NULL, status='deleted', updated_at=?", (now(),))
        conn.event("privacy.personal_data_deleted", data=out, actor="user")
    return out


def revoke_connection(conn: Conn, name: str) -> list[str]:
    """Remove stored credentials for a connected service ('mail' or an account id) and stop using it."""
    from . import secretstore

    prefix = "mail/" if name == "mail" else f"account/{name}"
    refs = [r["ref"] for r in conn.q("SELECT ref FROM secrets WHERE ref LIKE ?", (prefix + "%",))]
    for ref in refs:
        secretstore.delete(conn, ref)
    if name == "mail":
        dbmod.set_setting(conn, "mail_provider", None)
    else:
        conn.q("UPDATE accounts SET credential_ref=NULL, status='revoked', updated_at=? WHERE id=?", (now(), name))
    conn.event("connection.revoked", "connection", name, {"secrets_removed": len(refs)}, actor="user")
    return refs
