"""Command line entry point: python -m jobagent <command>."""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import sys
import time

from . import applications as apps, backup, config, db, pipeline, policy, profile, redact, secretstore, submission
from .sources import get_adapter, registry


def _conn():
    conn = db.connect()
    db.migrate(conn)
    policy.current(conn)
    return conn


def cmd_init(a):
    conn = _conn()
    v, _ = policy.current(conn)
    print(f"database ready ({'postgres' if conn.is_pg else 'sqlite'}), policy version {v}")


def cmd_keygen(a):
    print(secretstore.generate_master_key())
    print("Store this as JOBAGENT_MASTER_KEY in each runtime's secret settings. It is shown once and never saved by the agent.", file=sys.stderr)


def cmd_doctor(a):
    """Health check that proves connections work without printing any secret."""
    ok = True
    def line(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'ok' if good else 'FAIL'}] {name}{': ' + detail if detail else ''}")
    try:
        conn = _conn()
        line("database", True, ("postgres" if conn.is_pg else "sqlite") + f", {conn.val('SELECT COUNT(*) FROM schema_migrations')} migrations")
    except Exception as e:  # noqa: BLE001
        line("database", False, redact.redact(str(e))[:200])
        return 1
    try:
        tok = secretstore.encrypt(b"probe")
        line("master key", secretstore.decrypt(tok) == b"probe", "round trip ok")
        bad = 0
        for r in conn.q("SELECT ref, ciphertext FROM secrets"):
            try:
                secretstore.decrypt(r["ciphertext"])
            except secretstore.SecretError:
                bad += 1
        line("stored secrets readable", bad == 0, f"{conn.val('SELECT COUNT(*) FROM secrets')} stored, {bad} unreadable")
    except secretstore.SecretError as e:
        line("master key", False, str(e))
    line("profile", conn.val("SELECT COUNT(*) FROM profile_facts WHERE superseded_at IS NULL") > 0,
         f"{conn.val('SELECT COUNT(*) FROM profile_facts WHERE superseded_at IS NULL')} current facts, "
         f"{conn.val('SELECT COUNT(*) FROM documents')} documents")
    hb = db.get_setting(conn, "worker_heartbeat")
    line("worker heartbeat", True, hb["at"] if hb else "never (no worker has run)")
    from .mail import get_provider
    prov = get_provider(conn)
    if prov:
        try:
            line("mailbox", True, prov.check())
        except Exception as e:  # noqa: BLE001
            line("mailbox", False, type(e).__name__)
    else:
        print("[--] mailbox: not connected")
    s = db.all_settings(conn)
    print(f"[--] submission: enabled={s['submission_enabled']} dry_run={s['dry_run']} paused={s['global_pause']}")
    return 0 if ok else 1


def cmd_profile_import(a):
    conn = _conn()
    print(profile.import_profile(conn, json.load(open(a.file, encoding="utf-8"))))


def cmd_secret_set(a):
    conn = _conn()
    value = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass(f"value for {a.ref}: ")
    if not value:
        sys.exit("empty value")
    secretstore.put(conn, a.ref, value, a.scope)
    print(f"stored {a.ref} (scope {a.scope}, fingerprint {secretstore.fingerprint(value)})")


def cmd_import_applications(a):
    conn = _conn()
    if a.file.endswith(".csv"):
        rows = list(csv.DictReader(open(a.file, encoding="utf-8")))
    else:
        data = json.load(open(a.file, encoding="utf-8"))
        rows = data.get("applications", data) if isinstance(data, dict) else data
    n = sum(bool(apps.import_application(conn, {**r, "source": r.get("source") or a.source})) for r in rows if r.get("company"))
    print(f"imported or already present: {n} of {len(rows)}")


def cmd_discover(a):
    conn = _conn()
    ad = get_adapter(a.source)
    for board in (a.boards.split(",") if a.boards else ad.boards()):
        try:
            print(pipeline.discover_board(conn, ad, board))
        except Exception as e:  # noqa: BLE001
            print({"board": board, "error": f"{type(e).__name__}: {e}"})


def cmd_run_once(a):
    conn = _conn()
    print(json.dumps(pipeline.tick(conn, submission.worker_id()), indent=1, default=str))


def cmd_worker(a):
    redact.setup()
    conn = _conn()
    wid = submission.worker_id()
    redact.log().info("worker started", extra={"data": {"worker": wid}})
    while True:
        try:
            out = pipeline.tick(conn, wid)
            redact.log().info("tick", extra={"data": out})
        except Exception:  # noqa: BLE001
            redact.log().exception("tick failed")
        time.sleep(a.interval)


def cmd_status(a):
    conn = _conn()
    v, _ = policy.current(conn)
    print("settings:", db.all_settings(conn) | {"worker_heartbeat": (db.get_setting(conn, "worker_heartbeat") or {}).get("at")})
    print("policy version:", v)
    print("vacancies:", {r["status"]: r["n"] for r in conn.q("SELECT status, COUNT(*) AS n FROM vacancies GROUP BY status")})
    print("decisions:", {r["decision"]: r["n"] for r in conn.q("SELECT decision, COUNT(*) AS n FROM evaluations WHERE is_current=1 GROUP BY decision")})
    print("applications:", {r["state"]: r["n"] for r in conn.q("SELECT state, COUNT(*) AS n FROM applications GROUP BY state")})
    print("open tasks:", conn.val("SELECT COUNT(*) FROM user_tasks WHERE status='open'"))


def cmd_serve(a):
    import uvicorn
    from .web import create_app

    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")


def cmd_backup(a):
    print(backup.dump(_conn(), a.path))


def cmd_restore(a):
    print(backup.restore(db.connect(), a.path))


def cmd_set(a):
    conn = _conn()
    db.set_setting(conn, a.key, json.loads(a.value))
    print(db.all_settings(conn))


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    def add(name, fn, *args):
        sp = sub.add_parser(name, help=(fn.__doc__ or "").strip().split("\n")[0])
        for arg, kw in args:
            sp.add_argument(arg, **kw)
        sp.set_defaults(fn=fn)
    add("init", cmd_init)
    add("keygen", cmd_keygen)
    add("doctor", cmd_doctor)
    add("status", cmd_status)
    add("profile-import", cmd_profile_import, ("file", {}))
    add("secret-set", cmd_secret_set, ("ref", {}), ("--scope", {"required": True, "choices": sorted(secretstore.SCOPES)}))
    add("import-applications", cmd_import_applications, ("file", {}), ("--source", {"default": "manual import"}))
    add("discover", cmd_discover, ("--source", {"default": "greenhouse", "choices": sorted(registry())}), ("--boards", {"default": ""}))
    add("run-once", cmd_run_once)
    add("worker", cmd_worker, ("--interval", {"type": int, "default": 300}))
    add("serve", cmd_serve, ("--host", {"default": "127.0.0.1"}), ("--port", {"type": int, "default": 8787}))
    add("backup", cmd_backup, ("path", {}))
    add("restore", cmd_restore, ("path", {}))
    add("set", cmd_set, ("key", {"choices": sorted(db.DEFAULT_SETTINGS) + ["mail_provider", "mail_imap_host", "mail_imap_user", "mail_imap_folder"]}),
        ("value", {"help": "JSON value, e.g. true"}))
    a = p.parse_args(argv)
    sys.exit(a.fn(a) or 0)
