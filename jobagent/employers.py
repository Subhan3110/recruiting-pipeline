"""Employer identity: names, domains, source identifiers and aliases, with uncertainty preserved.

A shared applicant tracking system never merges employers: source aliases are
keyed by the tenant on that system (e.g. 'greenhouse:point72'), not the ATS host.
"""
from __future__ import annotations

import re
import unicodedata

from .db import Conn, new_id, now

_SUFFIX = re.compile(r"\b(asa|as|ab|aps|oy|oyj|gmbh|ag|se|sa|sas|bv|nv|plc|ltd|limited|llc|llp|lp|inc|incorporated|corp|corporation|"
                     r"co|company|group|holding|holdings|pte|pty|sarl|spa|srl|kk|nv)\b\.?", re.I)


def normalise(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").lower()
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("&", " and ")
    s = re.sub(r"(?<=[a-z])[./](?=[a-z]\b|\s|,|$)", "", s)  # "L.P." -> "lp", "A/S" -> "as"
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    prev = None
    while prev != s:  # strip trailing legal forms repeatedly: "X Holdings Ltd" -> "x"
        prev = s
        s = re.sub(rf"\s+{_SUFFIX.pattern}\s*$", "", s.strip(), flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def normalise_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    d = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0]
    return re.sub(r"^(www|careers|jobs)\.", "", d) or None


def find(conn: Conn, name: str | None = None, domain: str | None = None, source_key: str | None = None) -> dict | None:
    for typ, alias in (("source", source_key), ("domain", normalise_domain(domain)), ("name", normalise(name) if name else None)):
        if alias:
            row = conn.one("SELECT e.* FROM employer_aliases a JOIN employers e ON e.id=a.employer_id WHERE a.alias_type=? AND a.alias=?",
                           (typ, alias))
            if row:
                return row
    return None


def resolve(conn: Conn, name: str, domain: str | None = None, source_key: str | None = None,
            kind: str = "employer", provenance: str = "") -> dict:
    """Find or create an employer and attach any new aliases. Exact normalised matches only: no fuzzy merging."""
    with conn.tx(lock="employers"):
        emp = find(conn, name, domain, source_key)
        if not emp:
            eid = new_id("emp")
            conn.q("INSERT INTO employers (id, name, domain, kind, created_at) VALUES (?,?,?,?,?)",
                   (eid, name.strip(), normalise_domain(domain), kind, now()))
            conn.event("employer.created", "employer", eid, {"name": name}, provenance=provenance)
            emp = conn.one("SELECT * FROM employers WHERE id=?", (eid,))
        for typ, alias in (("name", normalise(name)), ("domain", normalise_domain(domain)), ("source", source_key)):
            if alias:
                add_alias(conn, emp["id"], typ, alias, provenance)
        return emp


def add_alias(conn: Conn, employer_id: str, alias_type: str, alias: str, provenance: str = "") -> bool:
    """Attach an alias. If it already belongs to a different employer, flag both as uncertain instead of merging."""
    if alias_type == "name":
        alias = normalise(alias)
    elif alias_type == "domain":
        alias = normalise_domain(alias) or ""
    if not alias:
        return False
    owner = conn.one("SELECT employer_id FROM employer_aliases WHERE alias_type=? AND alias=?", (alias_type, alias))
    if owner:
        if owner["employer_id"] != employer_id:
            conn.q("UPDATE employers SET identity_uncertain=1 WHERE id IN (?,?)", (employer_id, owner["employer_id"]))
            conn.event("employer.identity_conflict", "employer", employer_id, {"alias": alias, "other": owner["employer_id"]})
        return False
    conn.q("INSERT INTO employer_aliases (id, employer_id, alias_type, alias, provenance, created_at) VALUES (?,?,?,?,?,?)",
           (new_id("al"), employer_id, alias_type, alias, provenance, now()))
    return True


def merge(conn: Conn, keep_id: str, drop_id: str, actor: str = "user"):
    """User-confirmed merge of two employer records (e.g. a trading name and a legal name)."""
    with conn.tx(lock="employers"):
        for table in ("employer_aliases", "vacancies", "applications"):
            conn.q(f"UPDATE {table} SET employer_id=? WHERE employer_id=?", (keep_id, drop_id))
        conn.q("UPDATE employers SET parent_id=?, notes='merged' WHERE id=?", (keep_id, drop_id))
        conn.q("UPDATE employers SET identity_uncertain=0 WHERE id=?", (keep_id,))
        conn.event("employer.merged", "employer", keep_id, {"dropped": drop_id}, actor=actor)


def set_excluded(conn: Conn, employer_id: str, excluded: bool, reason: str = "", actor: str = "user"):
    conn.q("UPDATE employers SET excluded=?, exclusion_reason=? WHERE id=?", (1 if excluded else 0, reason, employer_id))
    conn.event("employer.excluded" if excluded else "employer.included", "employer", employer_id, {"reason": reason}, actor=actor)


def allow_additional(conn: Conn, employer_id: str, extra: int = 1, actor: str = "user"):
    conn.q("UPDATE employers SET extra_auto_allowed=extra_auto_allowed+? WHERE id=?", (extra, employer_id))
    conn.event("employer.additional_allowed", "employer", employer_id, {"extra": extra}, actor=actor)
