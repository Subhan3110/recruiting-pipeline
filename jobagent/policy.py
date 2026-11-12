"""Versioned standing application policy. Every change creates a new immutable version."""
from __future__ import annotations

import copy

from .db import Conn, j, now, unj

DEFAULT_POLICY = {
    "excluded_areas": [  # discoverable and rankable, but never submitted without individual approval
        {"name": "Oslo metropolitan area", "country": "NO",
         "places": ["oslo", "bærum", "lysaker", "fornebu", "sandvika", "asker", "billingstad", "høvik", "skøyen", "nydalen",
                    "lørenskog", "lillestrøm", "kolbotn", "nordre follo", "nittedal", "akershus"],
         "exceptions": []},
        {"name": "London metropolitan area", "country": "GB",
         "places": ["london", "greater london", "city of london", "canary wharf", "westminster", "croydon", "uxbridge", "wembley",
                    "ealing", "bromley", "hammersmith", "kingston upon thames", "richmond upon thames", "heathrow", "stratford, uk"],
         "exceptions": [r"london,? ?(on\b|ontario)", r"ontario", r"new london", r"londonderry", r"east london,? ?(za|south africa)",
                        r"south africa"]},
    ],
    "auto_submit_outside_excluded": True,
    "roles": {"min_score_auto": 60, "min_score_recommend": 40},
    "seniority": {"allowed": ["intern", "graduate", "entry"], "max_years_experience": 2},
    "eligibility": {
        "require_confirmed_work_authorisation": True,  # unknown authorisation holds the application
        "apply_when_sponsorship_needed": False,         # only where the vacancy does not rule sponsorship out, if True
    },
    "employers": {"max_auto_per_employer": 1},
    "limits": {"daily_submissions": 15, "per_source_daily": {"greenhouse": 10}, "min_seconds_between_submissions": 45},
    "accounts": {"create_accounts": True, "verify_by_email": True, "password_source": "generated",
                 "password_recovery": False, "max_failed_logins": 2},
    "verification": {"max_resends": 2, "code_ttl_minutes": 15, "poll_seconds": 180},
    "salary": {"minimum": None, "currency": None},
    "languages_for_documents": ["en"],
    "model": {"provider": "none", "monthly_budget_usd": 0},
    "retention": {"vacancy_raw_days": 180, "email_evidence_days": 365},
}


def current(conn: Conn) -> tuple[int, dict]:
    row = conn.one("SELECT version, config FROM policy_versions ORDER BY version DESC LIMIT 1")
    if not row:
        return save(conn, copy.deepcopy(DEFAULT_POLICY), "initial default policy", actor="system")
    return row["version"], unj(row["config"])


def save(conn: Conn, config: dict, note: str, actor: str = "user") -> tuple[int, dict]:
    validate(config)
    with conn.tx(lock="policy"):
        version = (conn.val("SELECT MAX(version) FROM policy_versions") or 0) + 1
        conn.q("INSERT INTO policy_versions (version, created_at, config, note, actor) VALUES (?,?,?,?,?)",
               (version, now(), j(config), note, actor))
        conn.event("policy.saved", "policy", str(version), {"note": note}, actor=actor)
    return version, config


def validate(cfg: dict):
    for area in cfg["excluded_areas"]:
        if not area.get("name") or not area.get("places"):
            raise ValueError("each excluded area needs a name and at least one place")
    if cfg["employers"]["max_auto_per_employer"] < 1:
        raise ValueError("max_auto_per_employer must be at least 1")
    if cfg["limits"]["daily_submissions"] < 0:
        raise ValueError("daily_submissions cannot be negative")


def history(conn: Conn) -> list[dict]:
    return conn.q("SELECT version, created_at, note, actor FROM policy_versions ORDER BY version DESC")
