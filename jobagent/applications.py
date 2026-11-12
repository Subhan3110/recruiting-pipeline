"""Application records and their explicit state machine.

One row per vacancy (UNIQUE vacancy_id): repeated attempts live under the same
application, so a second application to the same vacancy cannot exist.
"""
from __future__ import annotations

import hashlib

from . import answers as answers_mod, db, documents, employers, matching, policy as policy_mod, profile, vacancies
from .db import Conn, j, new_id, now, unj

STATES = ("discovered", "evaluated", "prepared", "awaiting_approval", "queued", "submitting", "submitted", "needs_input",
          "uncertain", "failed", "closed")
ALLOWED = {
    None: {"discovered", "evaluated", "submitted", "closed", "uncertain"},
    "discovered": {"evaluated", "closed"},
    "evaluated": {"prepared", "needs_input", "awaiting_approval", "closed", "evaluated"},
    "prepared": {"queued", "awaiting_approval", "needs_input", "closed", "evaluated", "prepared"},
    "awaiting_approval": {"queued", "prepared", "needs_input", "closed", "evaluated"},
    "queued": {"submitting", "prepared", "awaiting_approval", "needs_input", "closed", "evaluated"},
    "submitting": {"submitted", "uncertain", "needs_input", "failed", "queued", "prepared"},
    "needs_input": {"evaluated", "prepared", "queued", "awaiting_approval", "closed"},
    "uncertain": {"submitted", "queued", "closed"},      # only after reconciliation
    "failed": {"queued", "evaluated", "closed"},
    "submitted": {"closed"},
    "closed": {"evaluated"},
}
# States that count as "an application exists or may exist" for duplicate and employer limits.
OCCUPYING = ("submitting", "submitted", "uncertain")


class TransitionError(RuntimeError):
    pass


def transition(conn: Conn, app_id: str, to: str, reason: str = "", actor: str = "system", expect: str | None = None) -> bool:
    """Atomic compare-and-set state change with an audit row. Returns False if `expect` no longer holds."""
    with conn.tx():
        app = conn.one("SELECT state FROM applications WHERE id=?", (app_id,))
        if not app:
            raise TransitionError(f"unknown application {app_id}")
        if expect and app["state"] != expect:
            return False
        if to not in ALLOWED.get(app["state"], set()):
            raise TransitionError(f"{app['state']} -> {to} is not allowed")
        ts = now()
        rows = conn.q("UPDATE applications SET state=?, state_reason=?, updated_at=?" + (", submitted_at=?" if to == "submitted" else "")
                      + " WHERE id=? AND state=? RETURNING id",
                      (to, reason, ts) + ((ts,) if to == "submitted" else ()) + (app_id, app["state"]))
        if not rows:
            return False
        conn.q("INSERT INTO transitions (id, application_id, from_state, to_state, ts, reason, actor) VALUES (?,?,?,?,?,?,?)",
               (new_id("tr"), app_id, app["state"], to, ts, reason, actor))
        return True


def ensure(conn: Conn, vacancy_id: str) -> dict:
    row = conn.one("SELECT * FROM applications WHERE vacancy_id=?", (vacancy_id,))
    if row:
        return row
    v = conn.one("SELECT employer_id, title FROM vacancies WHERE id=?", (vacancy_id,))
    aid, ts = new_id("app"), now()
    conn.q("INSERT INTO applications (id, vacancy_id, employer_id, origin, state, role_title, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
           "ON CONFLICT (vacancy_id) DO NOTHING", (aid, vacancy_id, v["employer_id"], "agent", "evaluated", v["title"], ts, ts))
    row = conn.one("SELECT * FROM applications WHERE vacancy_id=?", (vacancy_id,))
    if row["id"] == aid:
        conn.q("INSERT INTO transitions (id, application_id, from_state, to_state, ts, reason, actor) VALUES (?,?,?,?,?,?,?)",
               (new_id("tr"), aid, None, "evaluated", ts, "selected for application", "system"))
    return row


def get(conn: Conn, app_id: str) -> dict | None:
    return conn.one("SELECT a.*, e.name AS employer_name FROM applications a JOIN employers e ON e.id=a.employer_id WHERE a.id=?", (app_id,))


def version(conn: Conn, app_id: str, ver: int | None = None) -> dict | None:
    row = conn.one("SELECT * FROM application_versions WHERE application_id=? " + ("AND version=?" if ver else "ORDER BY version DESC LIMIT 1"),
                   (app_id, ver) if ver else (app_id,))
    if row:
        for k in ("answers", "document_ids", "facts_snapshot"):
            row[k] = unj(row[k])
    return row


def content_hash(vacancy_id: str, resolved: dict, doc_shas: dict) -> str:
    material = {"vacancy": vacancy_id, "answers": [(a["field"], a["value"], a.get("option_value")) for a in resolved["answers"]],
                "documents": doc_shas, "location": resolved.get("selected_location")}
    return hashlib.sha256(j(material).encode()).hexdigest()


def prepare(conn: Conn, app_id: str, adapter, evaluation: dict | None = None) -> dict:
    """Build the exact application package (answers + documents) and route the application.

    Routing: missing information -> needs_input (with one task per gap);
    Oslo/London or an unselectable multi-location vacancy -> awaiting_approval; otherwise -> prepared.
    A changed package gets a new version; approvals are bound to a version hash.
    """
    app = get(conn, app_id)
    v = vacancies.get(conn, app["vacancy_id"])
    ev = evaluation or matching.current(conn, v["id"])
    ev_detail = ev["detail"]
    pv, _ = policy_mod.current(conn)
    src = next(s for s in v["sources"] if s["source"] == adapter.name)
    form = adapter.form(src["source_id"])
    if form is None:
        vacancies.close(conn, v["id"], "application form no longer available")
        transition(conn, app_id, "closed", "vacancy closed before preparation")
        return {"state": "closed"}
    resolved = answers_mod.resolve_form(conn, form, v, ev_detail["geo"], strict=True)
    docs, problems = documents.build_for(conn, v, ev_detail["relevance"]["categories"], resolved["needs"], strict=True)
    gaps = [f"{m['label']}" + (f" (options: {', '.join(m['options'][:12])})" if m.get("options") else "") for m in resolved["missing"]]
    for a in resolved["answers"]:
        if a["type"] == "input_file" and a["required"] and a["key"] not in docs:
            problems.append(f"required document '{a['key']}' could not be generated from confirmed facts")
    doc_shas = {k: conn.val("SELECT sha256 FROM documents WHERE id=?", (d,)) for k, d in docs.items()}
    chash = content_hash(v["id"], resolved, doc_shas)
    geo_decision = ev_detail["geo"]["decision"]
    needs_approval = geo_decision == "approval" or (geo_decision == "auto_if_selectable" and not resolved["location_selectable"])
    with conn.tx():
        cur = version(conn, app_id)
        if not cur or cur["content_hash"] != chash:
            ver = (cur["version"] if cur else 0) + 1
            conn.q("INSERT INTO application_versions (id, application_id, version, content_hash, answers, document_ids, facts_snapshot, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (new_id("ver"), app_id, ver, chash,
                                               j({"answers": resolved["answers"], "skipped": resolved["skipped"], "missing": resolved["missing"],
                                                  "form_url": form["url"], "tenant": form.get("tenant")}),
                                               j(docs), j(profile.snapshot(conn)), now()))
            conn.q("UPDATE approvals SET revoked_at=? WHERE application_id=? AND revoked_at IS NULL AND content_hash<>?", (now(), app_id, chash))
        else:
            ver = cur["version"]
        conn.q("UPDATE applications SET current_version=?, policy_version=?, requires_approval=?, selected_location=?, updated_at=? WHERE id=?",
               (ver, pv, 1 if needs_approval else 0, resolved["selected_location"], now(), app_id))
        state = conn.val("SELECT state FROM applications WHERE id=?", (app_id,))
        if state in OCCUPYING or state in ("submitted", "closed"):
            return {"state": state, "version": ver}
        if gaps or problems:
            for m in resolved["missing"]:
                profile.open_task(conn, "question", f"Answer needed: {m['label']}",
                                  f"Asked by {v['employer_name']} ({v['title']}). " + (f"Options: {', '.join(m['options'][:20])}. " if m.get("options") else "")
                                  + f"Reason: {m['why']}. Key: {m['key']}", "answer", m["key"],
                                  dedup_key=f"ans:{m['key']}:{'' if not m['key'].startswith('q:') else v['employer_id']}")
            for p in problems:
                profile.open_task(conn, "manual_action", f"Document problem: {p}", f"{v['employer_name']}: {v['title']}",
                                  "application", app_id, dedup_key=f"doc:{p[:60]}")
            target, reason = "needs_input", "; ".join((["missing answers: " + " | ".join(gaps)] if gaps else []) + problems)[:900]
        elif needs_approval:
            approved = conn.one("SELECT 1 FROM approvals WHERE application_id=? AND content_hash=? AND revoked_at IS NULL", (app_id, chash))
            target, reason = ("prepared", "approved; ready to queue") if approved else (
                "awaiting_approval", "; ".join(ev_detail["geo"]["reasons"]) or "individual approval required")
        else:
            target, reason = "prepared", "package complete"
        if state == "queued" and target == "prepared":
            target = "queued"  # re-checked just before submission and unchanged: stays queued
        elif state != target:
            transition(conn, app_id, target, reason)
    return {"state": target, "version": ver, "content_hash": chash, "missing": resolved["missing"], "problems": problems}


def approve(conn: Conn, app_id: str, actor: str = "user") -> bool:
    """Approve exactly the current prepared version of one application."""
    with conn.tx(lock="submission"):
        app = get(conn, app_id)
        ver = version(conn, app_id)
        if not app or app["state"] != "awaiting_approval" or not ver:
            return False
        conn.q("INSERT INTO approvals (id, application_id, content_hash, approved_at, actor) VALUES (?,?,?,?,?)",
               (new_id("apr"), app_id, ver["content_hash"], now(), actor))
        conn.event("application.approved", "application", app_id, {"version": ver["version"], "hash": ver["content_hash"]}, actor=actor)
        return transition(conn, app_id, "queued", f"approved version {ver['version']} by {actor}", actor)


def is_approved(conn: Conn, app_id: str, chash: str) -> bool:
    return conn.one("SELECT 1 FROM approvals WHERE application_id=? AND content_hash=? AND revoked_at IS NULL", (app_id, chash)) is not None


def queue_eligible(conn: Conn) -> list[str]:
    """Queue prepared, automatic-eligible applications: at most the strongest match per employer."""
    _, cfg = policy_mod.current(conn)
    rows = conn.q("SELECT a.id, a.employer_id, a.requires_approval, ev.score, ev.decision, e.extra_auto_allowed, e.excluded "
                  "FROM applications a JOIN evaluations ev ON ev.vacancy_id=a.vacancy_id AND ev.is_current=1 "
                  "JOIN employers e ON e.id=a.employer_id WHERE a.state='prepared' ORDER BY ev.score DESC, a.created_at")
    queued, per_emp = [], {}
    dry = db.get_setting(conn, "dry_run")
    for r in rows:
        if dry and conn.one("SELECT 1 FROM attempts t JOIN applications a ON a.id=t.application_id AND a.current_version=t.version "
                            "WHERE t.application_id=? AND t.outcome='dry_run'", (r["id"],)):
            continue  # this exact version has already been validated in dry-run mode
        if r["excluded"] or r["requires_approval"] or r["decision"] not in ("auto", "auto_if_selectable"):
            continue
        allowed = cfg["employers"]["max_auto_per_employer"] + r["extra_auto_allowed"]
        used = per_emp.get(r["employer_id"])
        if used is None:
            used = conn.val("SELECT COUNT(*) FROM applications WHERE employer_id=? AND state IN ('queued','submitting','submitted','uncertain')",
                            (r["employer_id"],))
        if used >= allowed:
            conn.q("UPDATE applications SET state_reason=? WHERE id=?",
                   ("held: a stronger match or an existing application already uses this employer's allowance", r["id"]))
            per_emp[r["employer_id"]] = used
            continue
        if transition(conn, r["id"], "queued", "eligible under standing policy; strongest match at this employer", expect="prepared"):
            queued.append(r["id"])
            per_emp[r["employer_id"]] = used + 1
    return queued


def import_application(conn: Conn, rec: dict, actor: str = "import") -> str:
    """Record an application made outside the agent. It counts toward every duplicate and employer restriction.

    rec: company, role, location?, status?, applied?(date), url?, source?, notes?, domain?
    """
    emp = employers.resolve(conn, rec["company"], rec.get("domain"), provenance=rec.get("source", "import"))
    status = (rec.get("status") or "Applied").strip().lower()
    state = {"preparing": None, "withdrawn": "closed", "rejected": "closed"}.get(status, "submitted")
    ext = f"{rec.get('source', 'import')}:{employers.normalise(rec['company'])}:{(rec.get('role') or '').strip().lower()}"
    with conn.tx(lock="submission"):
        existing = conn.one("SELECT id FROM applications WHERE external_ref=?", (ext,))
        if existing:
            return existing["id"]
        if state is None:
            return ""
        aid, ts = new_id("app"), now()
        applied = rec.get("applied") or ts
        conn.q("INSERT INTO applications (id, vacancy_id, employer_id, origin, state, state_reason, role_title, external_ref, created_at, updated_at, submitted_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (aid, None, emp["id"], "imported", state,
                                                 f"imported ({rec.get('status') or 'Applied'}): {rec.get('notes') or ''}"[:500],
                                                 rec.get("role"), ext, ts, ts, applied))
        conn.q("INSERT INTO transitions (id, application_id, from_state, to_state, ts, reason, actor) VALUES (?,?,?,?,?,?,?)",
               (new_id("tr"), aid, None, state, ts, f"imported from {rec.get('source', 'manual entry')}", actor))
        # A prior application to the employer uses its allowance even when it was later rejected or withdrawn.
        slot = (conn.val("SELECT MAX(slot) FROM employer_slots WHERE employer_id=?", (emp["id"],)) or 0) + 1
        conn.q("INSERT INTO employer_slots (employer_id, slot, application_id, taken_at) VALUES (?,?,?,?)", (emp["id"], slot, aid, ts))
        conn.event("application.imported", "application", aid, {"company": rec["company"], "status": rec.get("status")}, actor=actor,
                   provenance=rec.get("source", "manual entry"))
        return aid
