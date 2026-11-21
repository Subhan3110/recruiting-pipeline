"""Pipeline stages. Each stage is idempotent and driven by database state, so any worker can run any stage."""
from __future__ import annotations

from . import applications as apps, db, jobqueue, matching, policy as policy_mod, submission, vacancies, verification
from .db import Conn, now
from .mail import get_provider
from .redact import log
from .sources import get_adapter


def discover_board(conn: Conn, adapter, board: str) -> dict:
    seen, new = set(), 0
    for v in adapter.discover([board]):
        _, created = vacancies.upsert(conn, v)
        seen.add(v["source_id"])
        new += created
    closed = vacancies.mark_closed_missing(conn, adapter.name, f"{board}/", seen) if seen else 0
    conn.event("discovery.board_done", "source", f"{adapter.name}:{board}", {"seen": len(seen), "new": new, "closed": closed})
    return {"board": board, "seen": len(seen), "new": new, "closed": closed}


def enqueue_discovery(conn: Conn, adapter) -> int:
    n = 0
    for board in adapter.boards():
        n += bool(jobqueue.enqueue(conn, "discover", {"source": adapter.name, "board": board}, dedup_key=f"discover:{adapter.name}:{board}"))
    db.set_setting(conn, "last_discovery_at", now(), actor="system")
    return n


def run_jobs(conn: Conn, worker: str, adapters: dict, limit: int = 200) -> int:
    done = 0
    while done < limit:
        job = jobqueue.claim(conn, worker)
        if not job:
            break
        try:
            if job["kind"] == "discover":
                discover_board(conn, adapters[job["payload"]["source"]], job["payload"]["board"])
            jobqueue.complete(conn, job["id"])
        except Exception as exc:  # noqa: BLE001
            log().exception("job failed")
            jobqueue.fail(conn, job, f"{type(exc).__name__}: {exc}")
        done += 1
    return done


def evaluate_pending(conn: Conn, limit: int = 5000) -> int:
    """Evaluate new vacancies, and re-evaluate when the policy or the profile changed since the last evaluation."""
    pv, _ = policy_mod.current(conn)
    changed = conn.val("SELECT MAX(created_at) FROM profile_facts") or ""
    emp_changed = conn.val("SELECT MAX(ts) FROM events WHERE kind IN ('employer.excluded','employer.included','vacancy.dismissed')") or ""
    rows = conn.q("SELECT v.id FROM vacancies v LEFT JOIN evaluations ev ON ev.vacancy_id=v.id AND ev.is_current=1 "
                  "WHERE v.status='open' AND (ev.id IS NULL OR ev.policy_version<>? OR ev.created_at < ? OR ev.created_at < ?) LIMIT ?",
                  (pv, changed, emp_changed, limit))
    if not rows:
        return 0
    from .db import unj
    ids = [r["id"] for r in rows]
    # Load everything the evaluation reads in a handful of queries, then write all results in one transaction.
    # Against a remote database this turns ~15 round trips per vacancy into a few per batch.
    vac, raw = {}, {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for v in conn.q("SELECT v.*, e.name AS employer_name, e.excluded AS employer_excluded, e.identity_uncertain AS employer_uncertain "
                        f"FROM vacancies v JOIN employers e ON e.id=v.employer_id WHERE v.id IN ({marks})", chunk):
            v["locations"] = unj(v["locations"], [])
            v["sources"] = []
            vac[v["id"]] = v
        for srow in conn.q(f"SELECT vacancy_id, source, source_id, url, raw FROM vacancy_sources WHERE vacancy_id IN ({marks}) ORDER BY first_seen", chunk):
            vac[srow["vacancy_id"]]["sources"].append({k: srow[k] for k in ("source", "source_id", "url")})
            raw.setdefault(srow["vacancy_id"], srow["raw"])
    conn.cache = {"policy": policy_mod.current(conn), "vacancies": vac, "raw": raw, "writes": []}
    try:
        for vid in ids:
            matching.evaluate(conn, vid)
        writes = conn.cache["writes"]
    finally:
        conn.cache = None
    with conn.tx(lock="evaluations"):
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            conn.q(f"UPDATE evaluations SET is_current=0 WHERE is_current=1 AND vacancy_id IN ({','.join('?' * len(chunk))})", chunk)
        conn.many("INSERT INTO evaluations (id, vacancy_id, created_at, policy_version, score, decision, geo, rationale, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                  [w[0] for w in writes])
        conn.many("UPDATE vacancies SET locations=? WHERE id=?", [w[1] for w in writes])
        if any(w[2] for w in writes):
            from . import profile
            profile.open_task(conn, "question", "Work authorisation is not confirmed for some countries",
                              "Roles are held until you record, per country, whether you may work there and whether you need sponsorship.",
                              dedup_key="q:work_auth")
    return len(rows)


def prepare_pending(conn: Conn, adapter, per_employer: int = 2, limit: int = 40) -> int:
    """Prepare the strongest candidates per employer (each preparation costs one form request to the source)."""
    rows = conn.q("SELECT v.id, v.employer_id, ev.score, ev.created_at AS ev_at, a.id AS app_id, a.state, a.updated_at FROM vacancies v "
                  "JOIN evaluations ev ON ev.vacancy_id=v.id AND ev.is_current=1 "
                  "JOIN vacancy_sources vs ON vs.vacancy_id=v.id AND vs.source=? "
                  "LEFT JOIN applications a ON a.vacancy_id=v.id "
                  "WHERE v.status='open' AND ev.decision IN ('auto','auto_if_selectable','approval') ORDER BY ev.score DESC, v.first_seen",
                  (adapter.name,))
    answered = max(conn.val("SELECT MAX(created_at) FROM answers") or "", conn.val("SELECT MAX(created_at) FROM profile_facts") or "")
    per, n = {}, 0
    for r in rows:
        if n >= limit:
            break
        per[r["employer_id"]] = per.get(r["employer_id"], 0) + 1
        if per[r["employer_id"]] > per_employer:
            continue
        stale = r["state"] in ("needs_input", "prepared", "awaiting_approval") and r["updated_at"] < answered
        if r["app_id"] and r["state"] != "evaluated" and not stale:
            continue
        app = apps.ensure(conn, r["id"])
        try:
            apps.prepare(conn, app["id"], adapter)
            n += 1
        except Exception:  # noqa: BLE001
            log().exception("prepare failed")
    return n


def close_stale_applications(conn: Conn) -> int:
    rows = conn.q("SELECT a.id, a.state FROM applications a JOIN vacancies v ON v.id=a.vacancy_id "
                  "WHERE v.status='closed' AND a.state IN ('evaluated','prepared','awaiting_approval','queued','needs_input')")
    for r in rows:
        apps.transition(conn, r["id"], "closed", "vacancy closed at source", expect=r["state"])
    rej = conn.q("SELECT a.id, a.state FROM applications a JOIN evaluations ev ON ev.vacancy_id=a.vacancy_id AND ev.is_current=1 "
                 "WHERE ev.decision='reject' AND a.state IN ('evaluated','prepared','awaiting_approval','queued','needs_input')")
    for r in rej:
        apps.transition(conn, r["id"], "closed", "no longer eligible after re-evaluation", expect=r["state"])
    return len(rows) + len(rej)


def submit_pending(conn: Conn, adapter, worker: str, max_n: int = 3) -> list[tuple[str, str]]:
    out = []
    if submission.mode(conn) is None:
        return out
    provider = get_provider(conn)
    verifier = verification.make_verifier(conn, provider, adapter.mail_domains, adapter.allowed_hosts)
    rows = conn.q("SELECT a.id FROM applications a JOIN evaluations ev ON ev.vacancy_id=a.vacancy_id AND ev.is_current=1 "
                  "JOIN vacancy_sources vs ON vs.vacancy_id=a.vacancy_id AND vs.source=? WHERE a.state='queued' ORDER BY ev.score DESC LIMIT ?",
                  (adapter.name, max_n))
    for r in rows:
        out.append((r["id"], submission.submit_one(conn, r["id"], adapter, worker, verifier)))
    return out


def tick(conn: Conn, worker: str, discovery_interval_hours: float = 6) -> dict:
    """One full pass. Safe to run from several workers at once."""
    s = db.all_settings(conn)
    adapters = {"greenhouse": get_adapter("greenhouse")}
    out = {"recovered": submission.recover(conn, worker=worker), "jobs_recovered": jobqueue.recover(conn, worker=worker)}
    if s["discovery_enabled"]:
        last = db.get_setting(conn, "last_discovery_at")
        if not last or last < db.ago(hours=discovery_interval_hours):
            out["discovery_enqueued"] = sum(enqueue_discovery(conn, a) for a in adapters.values())
        out["jobs"] = run_jobs(conn, worker, adapters)
    out["evaluated"] = evaluate_pending(conn)
    out["closed"] = close_stale_applications(conn)
    if s["preparation_enabled"]:
        out["prepared"] = sum(prepare_pending(conn, a) for a in adapters.values())
    out["queued"] = len(apps.queue_eligible(conn))
    out["submissions"] = [x for a in adapters.values() for x in submit_pending(conn, a, worker)]
    provider = get_provider(conn)
    if provider:
        try:
            out["acknowledged"] = len(verification.reconcile_acknowledgements(conn, provider, adapters["greenhouse"].mail_domains))
        except Exception:  # noqa: BLE001
            log().exception("mail reconciliation failed")
    db.set_setting(conn, "worker_heartbeat", {"at": now(), "worker": worker}, actor="system")
    return out
