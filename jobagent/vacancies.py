"""Durable vacancy records, cross-source deduplication and repost detection."""
from __future__ import annotations

import hashlib
import html
import re

from . import employers
from .db import Conn, j, new_id, now, unj


def clean_text(s: str | None) -> str:
    """Vacancy text is untrusted input: strip markup, keep plain text only."""
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def fingerprint(title: str, location: str) -> str:
    """Stable key for 'the same job': normalised title + location, without ids, years-in-brackets noise or casing."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    loc = re.sub(r"[^a-z0-9 ]+", " ", (location or "").lower())
    key = re.sub(r"\s+", " ", f"{t}|{loc}").strip()
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def upsert(conn: Conn, v: dict) -> tuple[str, bool]:
    """Insert or refresh a vacancy from a source record. Returns (vacancy_id, created).

    v: source, source_id, employer_name, employer_domain?, employer_source_key?, title, url, description,
       location_raw, locations?, language?, deadline?, salary?, raw?
    """
    ts = now()
    with conn.tx(lock="vacancies"):
        src = conn.one("SELECT vacancy_id FROM vacancy_sources WHERE source=? AND source_id=?", (v["source"], str(v["source_id"])))
        desc = clean_text(v.get("description"))
        if src:
            conn.q("UPDATE vacancy_sources SET last_seen=?, url=?, raw=? WHERE source=? AND source_id=?",
                   (ts, v.get("url"), j(v.get("raw") or {}), v["source"], str(v["source_id"])))
            conn.q("UPDATE vacancies SET title=?, description=?, location_raw=?, locations=?, language=?, deadline=?, salary=?, "
                   "last_checked=?, status='open', closed_at=NULL WHERE id=?",
                   (v["title"], desc, v.get("location_raw"), j(v.get("locations") or []), v.get("language"), v.get("deadline"),
                    v.get("salary"), ts, src["vacancy_id"]))
            return src["vacancy_id"], False
        emp = employers.resolve(conn, v["employer_name"], v.get("employer_domain"), v.get("employer_source_key"),
                                provenance=f"{v['source']}:{v['source_id']}")
        fp = fingerprint(v["title"], v.get("location_raw") or "")
        twin = conn.one("SELECT id, status FROM vacancies WHERE employer_id=? AND fingerprint=? AND duplicate_of IS NULL ORDER BY first_seen LIMIT 1",
                        (emp["id"], fp))
        if twin and not conn.one("SELECT 1 FROM vacancy_sources WHERE vacancy_id=? AND source=?", (twin["id"], v["source"])):
            # Same job seen through another source: attach provenance, do not duplicate.
            conn.q("INSERT INTO vacancy_sources (id, vacancy_id, source, source_id, url, raw, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                   (new_id("vs"), twin["id"], v["source"], str(v["source_id"]), v.get("url"), j(v.get("raw") or {}), ts, ts))
            conn.event("vacancy.source_added", "vacancy", twin["id"], {"source": v["source"]})
            return twin["id"], False
        vid = new_id("vac")
        # Same employer, same title and location, new id on the same source: a repost or near-identical vacancy.
        conn.q("INSERT INTO vacancies (id, employer_id, title, canonical_url, description, location_raw, locations, language, deadline, "
               "salary, fingerprint, duplicate_of, needs_reconciliation, first_seen, last_checked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (vid, emp["id"], v["title"], v.get("url"), desc, v.get("location_raw"), j(v.get("locations") or []), v.get("language"),
                v.get("deadline"), v.get("salary"), fp, twin["id"] if twin else None, 1 if twin else 0, ts, ts))
        conn.q("INSERT INTO vacancy_sources (id, vacancy_id, source, source_id, url, raw, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
               (new_id("vs"), vid, v["source"], str(v["source_id"]), v.get("url"), j(v.get("raw") or {}), ts, ts))
        conn.event("vacancy.discovered", "vacancy", vid, {"repost_of": twin["id"] if twin else None},
                   provenance=f"{v['source']}:{v['source_id']}")
        return vid, True


def mark_closed_missing(conn: Conn, source: str, tenant_prefix: str, seen_ids: set[str]) -> int:
    """Close vacancies of one source tenant that no longer appear in its listing."""
    rows = conn.q("SELECT vs.vacancy_id, vs.source_id FROM vacancy_sources vs JOIN vacancies v ON v.id=vs.vacancy_id "
                  "WHERE vs.source=? AND vs.source_id LIKE ? AND v.status='open'", (source, tenant_prefix + "%"))
    n = 0
    for r in rows:
        if r["source_id"] not in seen_ids:
            close(conn, r["vacancy_id"], "no longer listed by source")
            n += 1
    return n


def close(conn: Conn, vacancy_id: str, reason: str):
    conn.q("UPDATE vacancies SET status='closed', closed_at=?, last_checked=? WHERE id=? AND status='open'", (now(), now(), vacancy_id))
    conn.event("vacancy.closed", "vacancy", vacancy_id, {"reason": reason})


def dismiss(conn: Conn, vacancy_id: str, reason: str = "", actor: str = "user"):
    conn.q("UPDATE vacancies SET dismissed_at=?, dismissed_reason=? WHERE id=?", (now(), reason, vacancy_id))
    conn.event("vacancy.dismissed", "vacancy", vacancy_id, {"reason": reason}, actor=actor)


def get(conn: Conn, vacancy_id: str) -> dict | None:
    v = conn.one("SELECT v.*, e.name AS employer_name, e.excluded AS employer_excluded, e.identity_uncertain AS employer_uncertain FROM vacancies v JOIN employers e ON e.id=v.employer_id WHERE v.id=?",
                 (vacancy_id,))
    if v:
        v["locations"] = unj(v["locations"], [])
        v["sources"] = conn.q("SELECT source, source_id, url FROM vacancy_sources WHERE vacancy_id=?", (vacancy_id,))
    return v
