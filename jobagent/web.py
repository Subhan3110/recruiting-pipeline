"""Local web interface. Server-rendered, autoescaped (vacancy and e-mail text is untrusted), no passwords ever shown."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import (accounts, answers, applications as apps, backup, config, db, documents, employers, geo, policy, profile,
               secretstore, submission, vacancies)
from .db import unj

TPL = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))
TPL.filters["unj"] = lambda s: unj(s, {})
TPL.filters["short"] = lambda s: (s or "")[:16].replace("T", " ")
TABS = ["overview", "jobs", "applications", "actions", "employers", "accounts", "profile", "policy", "import"]


def create_app() -> FastAPI:
    app = FastAPI(title="Job agent", docs_url=None, redoc_url=None)

    def conn():
        c = db.connect()
        db.migrate(c)
        policy.current(c)
        return c

    @app.middleware("http")
    async def auth(request: Request, call_next):
        token = config.ui_token()
        if token and request.url.path != "/healthz":
            supplied = request.query_params.get("token") or request.cookies.get("jobagent_token")
            if supplied != token:
                return Response("unauthorised", status_code=401)
            resp = await call_next(request)
            resp.set_cookie("jobagent_token", token, httponly=True, samesite="strict")
            return resp
        return await call_next(request)

    @app.get("/healthz")
    def healthz():
        try:
            c = conn()
            hb = db.get_setting(c, "worker_heartbeat") or {}
            return {"ok": True, "database": "postgres" if c.is_pg else "sqlite", "worker_heartbeat": hb.get("at"),
                    "mode": submission.mode(c) or "paused"}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)

    def back(tab: str, msg: str = ""):
        return RedirectResponse(f"/?tab={tab}" + (f"&msg={msg}" if msg else ""), status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(tab: str = "overview", msg: str = "", state: str = "", decision: str = ""):
        c = conn()
        pv, cfg = policy.current(c)
        ctx = {"tab": tab if tab in TABS else "overview", "tabs": TABS, "msg": msg, "settings": db.all_settings(c), "pv": pv, "cfg": cfg,
               "mode": submission.mode(c) or "paused", "tasks": profile.open_tasks(c), "state_filter": state, "decision_filter": decision,
               "heartbeat": (db.get_setting(c, "worker_heartbeat") or {}).get("at")}
        ctx["counts"] = {
            "vacancies": c.val("SELECT COUNT(*) FROM vacancies WHERE status='open'"),
            "decisions": {r["decision"]: r["n"] for r in c.q("SELECT decision, COUNT(*) AS n FROM evaluations ev JOIN vacancies v ON v.id=ev.vacancy_id "
                                                             "WHERE ev.is_current=1 AND v.status='open' GROUP BY decision")},
            "applications": {r["state"]: r["n"] for r in c.q("SELECT state, COUNT(*) AS n FROM applications GROUP BY state")},
            "unconfirmed": c.val("SELECT COUNT(*) FROM profile_facts WHERE superseded_at IS NULL AND status IN ('unconfirmed','proposed')"),
        }
        if ctx["tab"] == "jobs":
            want = decision or "open"
            cond = "ev.decision <> 'reject'" if want == "open" else "ev.decision = ?"
            ctx["jobs"] = c.q("SELECT v.id, v.title, v.location_raw, v.canonical_url, v.needs_reconciliation, e.name AS employer, ev.score, ev.decision, "
                              "ev.rationale, a.state AS app_state FROM evaluations ev JOIN vacancies v ON v.id=ev.vacancy_id "
                              "JOIN employers e ON e.id=v.employer_id LEFT JOIN applications a ON a.vacancy_id=v.id "
                              f"WHERE ev.is_current=1 AND v.status='open' AND v.dismissed_at IS NULL AND {cond} ORDER BY ev.score DESC LIMIT 200",
                              () if want == "open" else (want,))
        if ctx["tab"] == "applications":
            rows = c.q("SELECT a.*, e.name AS employer, v.title, v.location_raw, v.canonical_url, ev.score FROM applications a "
                       "JOIN employers e ON e.id=a.employer_id LEFT JOIN vacancies v ON v.id=a.vacancy_id "
                       "LEFT JOIN evaluations ev ON ev.vacancy_id=a.vacancy_id AND ev.is_current=1 "
                       + ("WHERE a.state=? " if state else "") + "ORDER BY a.updated_at DESC LIMIT 300", (state,) if state else ())
            ctx["apps"] = rows
        if ctx["tab"] == "employers":
            ctx["employers"] = c.q("SELECT e.*, (SELECT COUNT(*) FROM applications a WHERE a.employer_id=e.id AND a.state IN ('submitted','uncertain','submitting')) AS applied, "
                                   "(SELECT COUNT(*) FROM vacancies v WHERE v.employer_id=e.id AND v.status='open') AS open_vacancies "
                                   "FROM employers e WHERE e.parent_id IS NULL ORDER BY applied DESC, e.name LIMIT 500")
        if ctx["tab"] == "accounts":
            ctx["accounts"] = accounts.listing(c)
            ctx["secrets"] = secretstore.listing(c)
            ctx["mail_provider"] = db.get_setting(c, "mail_provider")
        if ctx["tab"] == "profile":
            ctx["facts"] = profile.facts(c)
            ctx["comments"] = profile.comments(c)
            ctx["answers"] = answers.library(c)
            held = set()
            for r in c.q("SELECT detail FROM evaluations WHERE is_current=1 AND decision='hold'"):
                held |= set((unj(r["detail"], {}).get("checks", {}).get("work_authorisation", {}) or {}).get("unknown", []))
            ctx["auth_needed"] = sorted(held)
        if ctx["tab"] == "policy":
            ctx["policy_json"] = json.dumps(cfg, indent=2, ensure_ascii=False)
            ctx["policy_history"] = policy.history(c)
        return TPL.get_template("index.html").render(**ctx)

    @app.get("/application/{app_id}", response_class=HTMLResponse)
    def application(app_id: str):
        c = conn()
        a = apps.get(c, app_id)
        if not a:
            raise HTTPException(404)
        v = vacancies.get(c, a["vacancy_id"]) if a["vacancy_id"] else None
        ev = c.one("SELECT * FROM evaluations WHERE vacancy_id=? AND is_current=1", (a["vacancy_id"],)) if v else None
        vers = c.q("SELECT version, content_hash, created_at FROM application_versions WHERE application_id=? ORDER BY version DESC", (app_id,))
        ver = apps.version(c, app_id)
        docs = [c.one("SELECT id, kind, filename, sha256, created_at FROM documents WHERE id=?", (d,)) for d in (ver["document_ids"].values() if ver else [])]
        return TPL.get_template("application.html").render(
            a=a, v=v, ev=ev, ver=ver, vers=vers, docs=[d for d in docs if d],
            transitions=c.q("SELECT * FROM transitions WHERE application_id=? ORDER BY ts", (app_id,)),
            attempts=c.q("SELECT * FROM attempts WHERE application_id=? ORDER BY started_at", (app_id,)),
            approvals=c.q("SELECT * FROM approvals WHERE application_id=? ORDER BY approved_at", (app_id,)),
            mails=c.q("SELECT kind, message_ref, received_at, sender_domain FROM email_messages WHERE application_id=?", (app_id,)))

    @app.get("/document/{doc_id}")
    def document(doc_id: str):
        try:
            meta, content = documents.load(conn(), doc_id)
        except KeyError:
            raise HTTPException(404)
        return Response(content, media_type=meta["mime"], headers={"Content-Disposition": f'inline; filename="{meta["filename"]}"'})

    # -- actions ---------------------------------------------------------------
    @app.post("/setting")
    def setting(key: str = Form(...), value: str = Form(...)):
        if key not in db.DEFAULT_SETTINGS:
            raise HTTPException(400)
        c = conn()
        val = value == "true"
        if key == "dry_run" and not val or key == "submission_enabled" and val:
            n = c.val("SELECT COUNT(*) FROM profile_facts WHERE superseded_at IS NULL AND status='unconfirmed'")
            if n:
                return back("overview", f"Live submission stays off: {n} profile facts are still unconfirmed")
        db.set_setting(c, key, val)
        return back("overview")

    @app.post("/approve")
    def approve(app_id: str = Form(...)):
        ok = apps.approve(conn(), app_id)
        return back("applications", "Approved and queued" if ok else "Could not approve: the application changed, reload and review")

    @app.post("/application/state")
    def app_state(app_id: str = Form(...), action: str = Form(...)):
        c = conn()
        if action == "close":
            apps.transition(c, app_id, "closed", "closed by user", "user")
        elif action == "queue":
            a = apps.get(c, app_id)
            if a["requires_approval"]:
                return back("applications", "This application needs approval, not queueing")
            apps.transition(c, app_id, "queued", "queued by user", "user")
        elif action in ("confirm_submitted", "confirm_not_submitted"):
            submission.resolve_uncertain(c, app_id, action == "confirm_submitted", {"kind": "checked by user"})
        elif action == "retry":
            apps.transition(c, app_id, "evaluated", "retry requested by user", "user")
        return back("applications")

    @app.post("/vacancy/dismiss")
    def dismiss(vacancy_id: str = Form(...), reason: str = Form("")):
        vacancies.dismiss(conn(), vacancy_id, reason)
        return back("jobs")

    @app.post("/employer")
    def employer(employer_id: str = Form(...), action: str = Form(...), reason: str = Form("")):
        c = conn()
        if action in ("exclude", "include"):
            employers.set_excluded(c, employer_id, action == "exclude", reason)
        elif action == "allow":
            employers.allow_additional(c, employer_id)
        elif action == "distinct":
            employers.confirm_distinct(c, employer_id)
        elif action == "merge" and reason:
            employers.merge(c, reason.strip(), employer_id)
        return back("employers")

    @app.post("/fact")
    def fact(key: str = Form(...), value: str = Form(""), action: str = Form("save"), note: str = Form("")):
        c = conn()
        if action == "confirm":
            profile.confirm_fact(c, key)
        elif action == "confirm_all":
            for f in profile.facts(c):
                if f["status"] == "unconfirmed":
                    profile.confirm_fact(c, f["key"])
        elif action == "retire":
            profile.retire_fact(c, key)
        else:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
            profile.set_fact(c, key.strip(), parsed, "confirmed", "entered by user in the interface", note or None)
        return back("profile")

    @app.post("/workauth")
    def workauth(country: str = Form(...), authorised: str = Form("no"), sponsorship: str = Form("no"), basis: str = Form("")):
        profile.set_fact(conn(), f"work_auth/{country.strip().upper()}", {"authorised": authorised == "yes", "needs_sponsorship": sponsorship == "yes",
                                                                         "basis": basis}, "confirmed", "entered by user in the interface")
        return back("profile")

    @app.post("/comment")
    def comment(text: str = Form(...)):
        profile.add_comment(conn(), text, "entered by user in the interface")
        return back("profile")

    @app.post("/answer")
    def answer(key: str = Form(...), answer: str = Form(...), scope_type: str = Form("global"), scope_value: str = Form(""),
               task_id: str = Form("")):
        c = conn()
        answers.set_answer(c, key, answer, scope_type, scope_value if scope_type != "global" else "", "confirmed", "entered by user in the interface")
        if task_id:
            profile.resolve_task(c, task_id, "answered")
        return back("actions" if task_id else "profile")

    @app.post("/task")
    def task(task_id: str = Form(...), status: str = Form("done"), resolution: str = Form("")):
        profile.resolve_task(conn(), task_id, resolution, status)
        return back("actions")

    @app.post("/policy")
    def save_policy(config_json: str = Form(...), note: str = Form("edited in the interface")):
        try:
            policy.save(conn(), json.loads(config_json), note)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return back("policy", f"Not saved: {e}")
        return back("policy", "Saved as a new policy version")

    @app.post("/import")
    def import_apps(payload: str = Form(...), source: str = Form("manual import")):
        c = conn()
        text = payload.strip()
        try:
            data = json.loads(text)
            rows = data.get("applications", data) if isinstance(data, dict) else data
        except json.JSONDecodeError:
            rows = list(csv.DictReader(io.StringIO(text)))
        n = sum(bool(apps.import_application(c, {**r, "source": r.get("source") or source}, "user")) for r in rows if r.get("company"))
        return back("import", f"Imported or already present: {n} of {len(rows)}")

    @app.post("/revoke")
    def revoke(name: str = Form(...)):
        backup.revoke_connection(conn(), name)
        return back("accounts", "Access revoked and stored credentials deleted")

    return app
