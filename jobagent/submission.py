"""Submission engine: final gate, cross-worker claim, attempt journal, recovery and reconciliation.

Invariants
- A submit click is journalled (attempt.submit_clicked_at) *before* the click happens.
- An attempt whose click was journalled but not confirmed is 'uncertain' and blocks further attempts
  to that vacancy and occupies the employer's allowance until it is reconciled.
- The gate and the claim run inside one serialised critical section ('submission' lock) and the
  employer allowance is additionally enforced by the UNIQUE key on employer_slots.
"""
from __future__ import annotations

import socket
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import applications as apps, db, matching, policy as policy_mod, profile, vacancies
from .db import Conn, j, new_id, now, unj
from .redact import log


@dataclass
class SubmitResult:
    outcome: str                      # confirmed | uncertain | not_submitted | blocked | dry_run
    evidence: dict = field(default_factory=dict)
    error: str = ""
    blocker: str = ""                 # captcha | verification | validation | login | closed | ...


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def mode(conn: Conn) -> str | None:
    """'live', 'dry_run' or None (nothing may run)."""
    s = db.all_settings(conn)
    if s["global_pause"]:
        return None
    if s["dry_run"]:
        return "dry_run"
    return "live" if s["submission_enabled"] else None


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def limits_block(conn: Conn, cfg: dict, source: str) -> str | None:
    lim = cfg["limits"]
    day = _today()
    total = conn.val("SELECT COUNT(*) FROM attempts WHERE mode='live' AND submit_clicked_at >= ?", (day,))
    if total >= lim["daily_submissions"]:
        return f"daily submission limit reached ({total}/{lim['daily_submissions']})"
    per = lim.get("per_source_daily", {}).get(source)
    if per is not None:
        n = conn.val("SELECT COUNT(*) FROM attempts t JOIN applications a ON a.id=t.application_id "
                     "JOIN vacancy_sources vs ON vs.vacancy_id=a.vacancy_id WHERE t.mode='live' AND t.submit_clicked_at >= ? AND vs.source=?",
                     (day, source))
        if n >= per:
            return f"daily limit for {source} reached ({n}/{per})"
    last = conn.val("SELECT MAX(submit_clicked_at) FROM attempts WHERE mode='live'")
    if last:
        gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if gap < lim["min_seconds_between_submissions"]:
            return "pacing: too soon after the previous submission"
    return None


def refresh(conn: Conn, app_id: str, adapter) -> tuple[bool, str]:
    """Outside the lock: re-check availability, policy, location and required answers against live data."""
    app = apps.get(conn, app_id)
    v = vacancies.get(conn, app["vacancy_id"])
    src = next((s for s in v["sources"] if s["source"] == adapter.name), None)
    if not src:
        return False, "no source for this adapter"
    open_ = adapter.is_open(src["source_id"])
    if open_ is False:
        vacancies.close(conn, v["id"], "closed at source before submission")
        apps.transition(conn, app_id, "closed", "vacancy closed before submission", expect="queued")
        return False, "vacancy closed"
    if open_ is None:
        return False, "could not confirm the vacancy is still open; will retry"
    ev = matching.evaluate(conn, v["id"])
    if ev["decision"] in ("reject", "hold", "review"):
        target = "closed" if ev["decision"] == "reject" else "needs_input" if ev["decision"] == "hold" else "prepared"
        apps.transition(conn, app_id, target, "re-check before submission: " + ev["rationale"][:400], expect="queued")
        return False, f"no longer eligible ({ev['decision']})"
    before = apps.version(conn, app_id)
    res = apps.prepare(conn, app_id, adapter, {"detail": ev["detail"]})
    after = apps.version(conn, app_id)
    if res["state"] != "queued":
        return False, f"moved to {res['state']} on re-check"
    if before and after["content_hash"] != before["content_hash"] and apps.get(conn, app_id)["requires_approval"]:
        apps.transition(conn, app_id, "awaiting_approval", "prepared application changed after approval; renewed review required", expect="queued")
        return False, "package changed after approval"
    return True, "ok"


def claim(conn: Conn, app_id: str, worker: str, source: str) -> tuple[dict | None, str]:
    """Inside the lock: final policy/history gate, employer slot, state change and attempt journal. All or nothing."""
    with conn.tx(lock="submission"):
        m = mode(conn)
        if m is None:
            return None, "submissions are paused or not enabled"
        pv, cfg = policy_mod.current(conn)
        app = apps.get(conn, app_id)
        if not app or app["state"] != "queued":
            return None, "application is no longer queued"
        ver = apps.version(conn, app_id)
        if not ver or ver["version"] != app["current_version"] or ver["answers"].get("missing"):
            return None, "no complete prepared version"
        emp = conn.one("SELECT * FROM employers WHERE id=?", (app["employer_id"],))
        if emp["excluded"]:
            apps.transition(conn, app_id, "closed", "employer excluded")
            return None, "employer excluded"
        if app["policy_version"] != pv:
            return None, "policy changed since preparation; will re-check"
        approved = apps.is_approved(conn, app_id, ver["content_hash"])
        if app["requires_approval"] and not approved:
            apps.transition(conn, app_id, "awaiting_approval", "approval missing for this version")
            return None, "approval required"
        if conn.one("SELECT 1 FROM attempts WHERE application_id=? AND (outcome IS NULL OR outcome='uncertain')", (app_id,)):
            return None, "an earlier attempt for this vacancy is unresolved"
        if m == "live":
            block = limits_block(conn, cfg, source)
            if block:
                return None, block
            # Employer allowance. Approved Oslo/London applications are explicit user decisions and take an extra slot.
            taken = [r["slot"] for r in conn.q("SELECT slot FROM employer_slots WHERE employer_id=? ORDER BY slot", (app["employer_id"],))]
            allowed = cfg["employers"]["max_auto_per_employer"] + emp["extra_auto_allowed"]
            if len(taken) >= allowed and not approved:
                apps.transition(conn, app_id, "prepared", "employer allowance used: an application to this employer already exists or is unresolved")
                return None, "employer allowance used"
            conn.q("INSERT INTO employer_slots (employer_id, slot, application_id, taken_at) VALUES (?,?,?,?)",
                   (app["employer_id"], (max(taken) if taken else 0) + 1, app_id, now()))
        attempt = new_id("att")
        conn.q("INSERT INTO attempts (id, application_id, version, policy_version, worker, mode, started_at) VALUES (?,?,?,?,?,?,?)",
               (attempt, app_id, ver["version"], pv, worker, m, now()))
        conn.q("UPDATE applications SET claimed_by=?, claimed_at=? WHERE id=?", (worker, now(), app_id))
        if not apps.transition(conn, app_id, "submitting", f"claimed by {worker} ({m})", expect="queued"):
            raise RuntimeError("lost the claim race")  # rolls back the slot and attempt
        return {"attempt_id": attempt, "mode": m, "version": ver, "app": app, "policy_version": pv}, "claimed"


class Hooks:
    """Callbacks the browser flow uses; they keep the journal and the pause switch authoritative."""

    def __init__(self, conn: Conn, attempt_id: str, mode_: str, verifier=None):
        self.conn, self.attempt_id, self.mode, self.verifier = conn, attempt_id, mode_, verifier

    def before_submit(self) -> bool:
        """Last check, then journal the click. Returns False if the click must not happen."""
        with self.conn.tx(lock="submission"):
            if self.mode != "live" or mode(self.conn) != "live":
                return False
            self.conn.q("UPDATE attempts SET submit_clicked_at=? WHERE id=? AND submit_clicked_at IS NULL", (now(), self.attempt_id))
            return True

    def verification_code(self, portal_origin: str, employer_hint: str, recipient: str, application_id: str) -> str | None:
        return self.verifier(portal_origin, employer_hint, recipient, application_id) if self.verifier else None


def finish(conn: Conn, claim_info: dict, result: SubmitResult):
    app_id, attempt = claim_info["app"]["id"], claim_info["attempt_id"]
    clicked = conn.val("SELECT submit_clicked_at FROM attempts WHERE id=?", (attempt,))
    outcome = result.outcome
    # After a journalled click only explicit portal evidence (visible form errors, a code prompt) proves nothing was sent.
    if outcome in ("not_submitted", "blocked") and clicked and not result.evidence.get("not_sent"):
        outcome = "uncertain"  # a failure after the click may hide a successful submission
    with conn.tx(lock="submission"):
        conn.q("UPDATE attempts SET finished_at=?, outcome=?, evidence=?, error=? WHERE id=?",
               (now(), outcome, j(result.evidence), (result.error or "")[:1000], attempt))
        title = f"{claim_info['app']['employer_name']}: {claim_info['app']['role_title']}"
        if outcome == "confirmed":
            apps.transition(conn, app_id, "submitted", "confirmed: " + (result.evidence.get("kind") or "portal confirmation"))
        elif outcome == "uncertain":
            apps.transition(conn, app_id, "uncertain", "submit was clicked but no confirmation was captured: " + (result.error or "")[:300])
            profile.open_task(conn, "reconcile", f"Uncertain submission: {title}",
                              "The submit button was clicked without a captured confirmation. The agent will look for an acknowledgement "
                              "e-mail. No new attempt will be made until this is resolved.", "application", app_id, dedup_key=f"unc:{app_id}")
        elif outcome == "dry_run":
            apps.transition(conn, app_id, "prepared", "dry run completed: form filled and validated, submit not clicked")
        elif outcome == "blocked":
            _release(conn, app_id)
            apps.transition(conn, app_id, "needs_input", f"blocked by {result.blocker}: {result.error}"[:500])
            profile.open_task(conn, "manual_action", f"Action needed ({result.blocker}): {title}", result.error or "",
                              "application", app_id, dedup_key=f"blk:{app_id}:{result.blocker}")
        else:  # not_submitted before any click: safe to retry a bounded number of times
            _release(conn, app_id)
            tries = conn.val("SELECT COUNT(*) FROM attempts WHERE application_id=? AND outcome='not_submitted'", (app_id,))
            if tries >= 3:
                apps.transition(conn, app_id, "failed", f"gave up after {tries} failed attempts: {result.error}"[:500])
                profile.open_task(conn, "manual_action", f"Submission keeps failing: {title}", result.error or "", "application", app_id,
                                  dedup_key=f"fail:{app_id}")
            else:
                apps.transition(conn, app_id, "queued", f"attempt failed before submit (try {tries}): {result.error}"[:500])
        conn.event("attempt.finished", "application", app_id, {"attempt": attempt, "outcome": outcome, "mode": claim_info["mode"]})


def _release(conn: Conn, app_id: str):
    conn.q("DELETE FROM employer_slots WHERE application_id=?", (app_id,))


def submit_one(conn: Conn, app_id: str, adapter, worker: str | None = None, verifier=None) -> str:
    """Full path for one queued application. Returns a short status string."""
    worker = worker or worker_id()
    if mode(conn) is None:
        return "paused"
    ok, why = refresh(conn, app_id, adapter)
    if not ok:
        return why
    info, why = claim(conn, app_id, worker, adapter.name)
    if not info:
        return why
    v = vacancies.get(conn, info["app"]["vacancy_id"])
    package = build_package(conn, info, v)
    hooks = Hooks(conn, info["attempt_id"], info["mode"], verifier)
    try:
        result = adapter.submitter().submit(package, hooks)
    except Exception as exc:  # noqa: BLE001 - the journal decides whether this was before or after the click
        log().exception("submission crashed")
        result = SubmitResult("not_submitted", error=f"{type(exc).__name__}: {exc}")
    finish(conn, info, result)
    return result.outcome


def build_package(conn: Conn, info: dict, vacancy: dict) -> dict:
    from . import documents

    ver = info["version"]
    files = {}
    for kind, doc_id in ver["document_ids"].items():
        meta, content = documents.load(conn, doc_id)
        files[kind] = {"filename": meta["filename"], "mime": meta["mime"], "content": content, "sha256": meta["sha256"]}
    return {"url": ver["answers"]["form_url"], "tenant": ver["answers"].get("tenant"), "answers": ver["answers"]["answers"],
            "files": files, "employer": vacancy["employer_name"], "title": vacancy["title"], "application_id": info["app"]["id"],
            "email": next((a["value"] for a in ver["answers"]["answers"] if a["key"] == "email"), None),
            "phone_country": (profile.get_fact(conn, "contact/phone_country") or (profile.get_fact(conn, "contact/location") or {}).get("country"))}


def recover(conn: Conn, stale_minutes: int = 20, worker: str | None = None) -> dict:
    """After a crash or restart: no attempt is silently dropped.

    Claimed applications whose worker is gone are resolved from the journal:
    click journalled -> uncertain (never retried blindly); no click -> back to the queue.
    """
    out = {"uncertain": 0, "requeued": 0}
    rows = conn.q("SELECT id, claimed_by, claimed_at FROM applications WHERE state='submitting' AND (claimed_at < ? OR claimed_by = ?)",
                  (db.ago(minutes=stale_minutes), worker or ""))
    for r in rows:
        with conn.tx(lock="submission"):
            att = conn.one("SELECT * FROM attempts WHERE application_id=? AND outcome IS NULL ORDER BY started_at DESC LIMIT 1", (r["id"],))
            if att and att["submit_clicked_at"]:
                conn.q("UPDATE attempts SET outcome='uncertain', finished_at=?, error='worker stopped after the submit click' WHERE id=?",
                       (now(), att["id"]))
                apps.transition(conn, r["id"], "uncertain", "worker stopped after the submit click; outcome unknown")
                profile.open_task(conn, "reconcile", "Uncertain submission after a worker restart",
                                  "Check the portal or wait for the acknowledgement e-mail.", "application", r["id"], dedup_key=f"unc:{r['id']}")
                out["uncertain"] += 1
            else:
                if att:
                    conn.q("UPDATE attempts SET outcome='aborted', finished_at=?, error='worker stopped before the submit click' WHERE id=?",
                           (now(), att["id"]))
                _release(conn, r["id"])
                apps.transition(conn, r["id"], "queued", "worker stopped before the submit click; safe to retry")
                out["requeued"] += 1
    return out


def resolve_uncertain(conn: Conn, app_id: str, submitted: bool, evidence: dict, actor: str = "user") -> bool:
    """Close out an uncertain attempt with evidence (acknowledgement e-mail, portal status, or the user's own check)."""
    with conn.tx(lock="submission"):
        app = apps.get(conn, app_id)
        if not app or app["state"] != "uncertain":
            return False
        att = conn.one("SELECT id, evidence FROM attempts WHERE application_id=? AND outcome='uncertain' ORDER BY started_at DESC LIMIT 1", (app_id,))
        if att:
            merged = (unj(att["evidence"], {}) or {}) | {"reconciliation": evidence | {"by": actor, "at": now()}}
            conn.q("UPDATE attempts SET outcome=?, evidence=? WHERE id=?", ("confirmed" if submitted else "not_submitted", j(merged), att["id"]))
        if submitted:
            apps.transition(conn, app_id, "submitted", f"reconciled as submitted ({evidence.get('kind', 'manual check')})", actor)
        else:
            _release(conn, app_id)
            apps.transition(conn, app_id, "queued", f"reconciled as not submitted ({evidence.get('kind', 'manual check')}); may retry", actor)
        profile.resolve_tasks_by_key(conn, f"unc:{app_id}", "reconciled")
        return True
