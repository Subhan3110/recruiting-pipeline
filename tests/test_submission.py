"""Submission gate, approvals, duplicate prevention across workers, ambiguous outcomes and recovery.

Browser tests drive real Chromium against tests/fixtures/form_server.py, a local stand-in for the hosted form.
"""
import threading

import pytest

from jobagent import applications as apps, db, employers, matching, policy, profile, submission, verification
from jobagent.browser.greenhouse_form import GreenhouseFormSubmitter
from jobagent.mail import FixtureMail, MailMessage
from jobagent.submission import SubmitResult
from .conftest import FakeAdapter, add_vacancy, seed_profile
from .fixtures.form_server import CODE, FormServer


@pytest.fixture(scope="module")
def server():
    s = FormServer()
    yield s
    s.close()


def browser(timeout_ms=4000):
    return GreenhouseFormSubmitter(allowed_hosts=("127.0.0.1",), timeout_ms=timeout_ms)


def go_live(conn):
    _, cfg = policy.current(conn)
    cfg["limits"]["min_seconds_between_submissions"] = 0
    policy.save(conn, cfg, "test pacing")
    db.set_setting(conn, "dry_run", False)
    db.set_setting(conn, "submission_enabled", True)


def ready(conn, adapter, **vac):
    vid = add_vacancy(conn, **vac)
    matching.evaluate(conn, vid)
    app = apps.ensure(conn, vid)
    apps.prepare(conn, app["id"], adapter)
    return app["id"]


class StubSubmitter:
    """Scripted submitter for gate tests that do not need a browser."""
    def __init__(self, outcome="confirmed", click=True, crash=False):
        self.outcome, self.click, self.crash, self.calls = outcome, click, crash, 0

    def submit(self, package, hooks):
        self.calls += 1
        if hooks.mode != "live":
            return SubmitResult("dry_run")
        if self.click and not hooks.before_submit():
            return SubmitResult("not_submitted", error="paused")
        if self.crash:
            raise ConnectionError("connection reset after click")
        return SubmitResult(self.outcome, evidence={"kind": "confirmation page"} if self.outcome == "confirmed" else {})


def state(conn, app_id):
    return conn.val("SELECT state FROM applications WHERE id=?", (app_id,))


# -- real browser against the fixture form ------------------------------------------
def test_live_submission_is_confirmed_with_evidence(conn, server):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(server.url("ok"), browser())
    app_id = ready(conn, ad)
    assert apps.queue_eligible(conn) == [app_id]
    n = len(server.submissions)
    assert submission.submit_one(conn, app_id, ad, "w1") == "confirmed"
    sent = server.submissions[n]
    assert sent["first_name"] == "Test" and sent["email"] == "test.candidate@example.com" and sent["country"] == "Norway +47"
    assert sent["question_1"] == "Yes" and sent["question_2"] == "No" and sent["question_3"] == "Company website"
    assert sent["resume"].startswith("Test_Candidate_CV.pdf:") and sent["cover_letter"].startswith("Test_Candidate_Cover_Letter_Fixture_Bank.pdf:")
    att = conn.one("SELECT * FROM attempts WHERE application_id=?", (app_id,))
    assert state(conn, app_id) == "submitted" and att["outcome"] == "confirmed" and att["submit_clicked_at"]
    assert "Thank you for applying" in att["evidence"] and att["policy_version"] == policy.current(conn)[0]
    assert submission.submit_one(conn, app_id, ad, "w1") != "confirmed" and len(server.submissions) == n + 1  # never twice


def test_dry_run_fills_but_never_clicks(conn, server):
    seed_profile(conn)
    ad = FakeAdapter(server.url("ok"), browser())
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    n = len(server.submissions)
    assert submission.submit_one(conn, app_id, ad, "w1") == "dry_run"
    assert len(server.submissions) == n and state(conn, app_id) == "prepared"
    assert conn.one("SELECT submit_clicked_at FROM attempts WHERE application_id=?", (app_id,))["submit_clicked_at"] is None
    assert apps.queue_eligible(conn) == []  # the same version is not dry-run again


def test_ambiguous_outcome_blocks_retry_until_reconciled(conn, server):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(server.url("hang"), browser(2500))
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    assert submission.submit_one(conn, app_id, ad, "w1") == "uncertain"
    assert state(conn, app_id) == "uncertain" and conn.one("SELECT 1 FROM user_tasks WHERE kind='reconcile' AND status='open'")
    assert submission.claim(conn, app_id, "w2", "greenhouse")[0] is None
    # A sibling vacancy at the same employer is blocked while the outcome is unknown.
    other = ready(conn, FakeAdapter(server.url("ok"), browser()), title="AML Analyst Intern", sid="fixturebank/2")
    assert apps.queue_eligible(conn) == [] and state(conn, other) == "prepared"
    # The acknowledgement e-mail settles it.
    mail = FixtureMail([MailMessage("m1", db.now(), "no-reply@us.greenhouse-mail.io", ["test.candidate@example.com"],
                                    "Thank you for applying to Fixture Bank", "We have received your application for Compliance Analyst Intern.")])
    assert verification.reconcile_acknowledgements(conn, mail, ("greenhouse-mail.io",)) == [app_id]
    assert state(conn, app_id) == "submitted"
    ev = conn.one("SELECT evidence FROM attempts WHERE application_id=?", (app_id,))["evidence"]
    assert "acknowledgement e-mail" in ev and '"m1"' in ev and "We have received" not in ev  # reference only, no body


def test_emailed_security_code_flow(conn, server):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(server.url("code"), browser())
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    mail = FixtureMail()

    def deliver(_):  # the code arrives only after the portal asked for it (delivery delay)
        mail.messages.append(MailMessage("code-1", db.now(), "no-reply@us.greenhouse-mail.io", ["test.candidate@example.com"],
                                         "Security code for your application to Fixture Bank", f"Your security code is: {CODE}"))
    verifier = verification.make_verifier(conn, mail, ad.mail_domains, ad.allowed_hosts, wait_seconds=20, interval=0.2, sleep=deliver)
    n = len(server.submissions)
    assert submission.submit_one(conn, app_id, ad, "w1", verifier) == "confirmed"
    assert server.submissions[n]["security_code"] == CODE and state(conn, app_id) == "submitted"
    dump = str(conn.q("SELECT * FROM email_messages")) + str(conn.q("SELECT * FROM events")) + str(conn.q("SELECT * FROM attempts"))
    assert CODE not in dump and conn.val("SELECT status FROM verification_requests") == "consumed"


def test_missing_code_is_a_user_action_not_an_uncertain_submission(conn, server):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(server.url("code"), browser())
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    verifier = verification.make_verifier(conn, FixtureMail(), ad.mail_domains, ad.allowed_hosts, wait_seconds=1, interval=0.2)
    assert submission.submit_one(conn, app_id, ad, "w1", verifier) == "blocked"
    assert state(conn, app_id) == "needs_input" and conn.val("SELECT COUNT(*) FROM employer_slots") == 0


def test_form_validation_error_is_not_uncertain(conn, server):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(server.url("error"), browser())
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    assert submission.submit_one(conn, app_id, ad, "w1") == "blocked" and state(conn, app_id) == "needs_input"


def test_form_on_unexpected_host_is_refused(conn):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter("https://evil.example/apply", browser())
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    assert submission.submit_one(conn, app_id, ad, "w1") == "blocked"
    assert conn.one("SELECT submit_clicked_at FROM attempts")["submit_clicked_at"] is None


# -- gate logic (stub submitter) ------------------------------------------------------
def test_global_pause_and_disabled_submission(conn):
    seed_profile(conn); go_live(conn)
    stub = StubSubmitter()
    ad = FakeAdapter(submitter=stub)
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    db.set_setting(conn, "global_pause", True)
    assert submission.submit_one(conn, app_id, ad) == "paused" and stub.calls == 0 and state(conn, app_id) == "queued"
    db.set_setting(conn, "global_pause", False); db.set_setting(conn, "submission_enabled", False)
    assert submission.submit_one(conn, app_id, ad) == "paused" and stub.calls == 0


def test_pause_between_fill_and_click_stops_the_click(conn):
    seed_profile(conn); go_live(conn)

    class PauseMidway(StubSubmitter):
        def submit(self, package, hooks):
            db.set_setting(conn, "global_pause", True)  # user hits pause while the form is being filled
            return super().submit(package, hooks)
    ad = FakeAdapter(submitter=PauseMidway())
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    assert submission.submit_one(conn, app_id, ad) == "not_submitted"
    assert conn.one("SELECT submit_clicked_at FROM attempts")["submit_clicked_at"] is None and state(conn, app_id) == "queued"


def test_london_requires_approval_bound_to_the_version(conn):
    seed_profile(conn, work_auth=("GB", "NL")); go_live(conn)
    stub = StubSubmitter()
    ad = FakeAdapter(submitter=stub)
    app_id = ready(conn, ad, location="London, UK", employer="London Bank", sid="ldn/1")
    assert state(conn, app_id) == "awaiting_approval" and apps.queue_eligible(conn) == []
    # Even if something forces it into the queue, the gate refuses without an approval.
    conn.q("UPDATE applications SET state='queued' WHERE id=?", (app_id,))
    assert submission.claim(conn, app_id, "w1", "greenhouse")[0] is None and state(conn, app_id) == "awaiting_approval" and stub.calls == 0
    assert apps.approve(conn, app_id) and state(conn, app_id) == "queued"
    # A material change after approval (corrected phone number) needs renewed review.
    profile.set_fact(conn, "contact/phone", "+47 41111111", "confirmed", "user correction")
    assert submission.submit_one(conn, app_id, ad) != "confirmed" and state(conn, app_id) == "awaiting_approval" and stub.calls == 0
    assert apps.approve(conn, app_id)
    assert submission.submit_one(conn, app_id, ad) == "confirmed" and state(conn, app_id) == "submitted"


def test_multi_location_auto_only_with_selectable_eligible_location(conn):
    seed_profile(conn, work_auth=("AE", "GB", "NL"))
    plain = ready(conn, FakeAdapter(), location="London; Dubai", employer="Gulf Bank", sid="gulf/1")
    assert state(conn, plain) == "awaiting_approval"
    loc_q = FakeAdapter().form("x/1")["questions"][:5] + [{
        "label": "Which office are you applying to?", "description": "", "required": True,
        "fields": [{"name": "question_7", "type": "multi_value_single_select",
                    "options": [{"label": "London", "value": 1}, {"label": "Dubai", "value": 2}]}]}]
    pick = ready(conn, FakeAdapter(questions=loc_q), location="London; Dubai", employer="Emirates Capital", sid="emi/1")
    assert state(conn, pick) == "prepared" and apps.get(conn, pick)["selected_location"] == "Dubai"


def test_one_automatic_application_per_employer_strongest_first(conn):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(submitter=StubSubmitter())
    weak = ready(conn, ad, title="Treasury Analyst", sid="fixturebank/10", description="Liquidity and settlement. Bachelor degree.")
    strong = ready(conn, ad, title="AML Compliance Analyst Intern", sid="fixturebank/11")
    assert apps.queue_eligible(conn) == [strong] and state(conn, weak) == "prepared"
    assert submission.submit_one(conn, strong, ad) == "confirmed"
    assert apps.queue_eligible(conn) == []
    employers.allow_additional(conn, apps.get(conn, weak)["employer_id"])
    assert apps.queue_eligible(conn) == [weak] and submission.submit_one(conn, weak, ad) == "confirmed"


def test_two_workers_cannot_both_claim(conn, url):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(submitter=StubSubmitter())
    a = ready(conn, ad, sid="fixturebank/20")
    b = ready(conn, ad, title="KYC Analyst Intern", sid="fixturebank/21")
    conn.q("UPDATE applications SET state='queued' WHERE id IN (?,?)", (a, b))  # worst case: both queued for one employer
    results, barrier = [], threading.Barrier(4)

    def worker(i, app_id):
        c = db.connect(url)
        barrier.wait()
        try:
            results.append((app_id, submission.claim(c, app_id, f"w{i}", "greenhouse")[0] is not None))
        finally:
            c.close()
    threads = [threading.Thread(target=worker, args=(i, x)) for i, x in enumerate([a, a, b, b])]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sum(ok for _, ok in results) == 1                    # one claim in total: same vacancy and same employer
    assert conn.val("SELECT COUNT(*) FROM employer_slots") == 1 and conn.val("SELECT COUNT(*) FROM attempts") == 1


def test_restart_recovery_never_drops_an_attempt(conn):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(submitter=StubSubmitter())
    before = ready(conn, ad, sid="fixturebank/30")
    after = ready(conn, ad, employer="Other Bank", sid="other/1")
    conn.q("UPDATE applications SET state='queued' WHERE id IN (?,?)", (before, after))
    i1, _ = submission.claim(conn, before, "dead-worker", "greenhouse")
    i2, _ = submission.claim(conn, after, "dead-worker", "greenhouse")
    submission.Hooks(conn, i2["attempt_id"], "live").before_submit()   # crashed right after clicking submit
    out = submission.recover(conn, worker="dead-worker")
    assert out == {"uncertain": 1, "requeued": 1}
    assert state(conn, before) == "queued" and state(conn, after) == "uncertain"
    assert conn.val("SELECT outcome FROM attempts WHERE id=?", (i1["attempt_id"],)) == "aborted"
    assert submission.claim(conn, after, "w2", "greenhouse")[0] is None


def test_crash_after_click_is_uncertain_and_manual_resolution_requeues(conn):
    seed_profile(conn); go_live(conn)
    ad = FakeAdapter(submitter=StubSubmitter(crash=True))
    app_id = ready(conn, ad)
    apps.queue_eligible(conn)
    assert submission.submit_one(conn, app_id, ad) == "not_submitted"   # what the submitter saw
    assert state(conn, app_id) == "uncertain"                           # what the journal knows
    assert submission.resolve_uncertain(conn, app_id, False, {"kind": "checked portal: no application"})
    assert state(conn, app_id) == "queued" and conn.val("SELECT COUNT(*) FROM employer_slots") == 0


def test_closed_vacancy_and_policy_change_are_rechecked_before_submit(conn):
    seed_profile(conn); go_live(conn)
    stub = StubSubmitter()
    app_id = ready(conn, FakeAdapter(submitter=stub))
    apps.queue_eligible(conn)
    assert submission.submit_one(conn, app_id, FakeAdapter(submitter=stub, open_=False)) == "vacancy closed" and state(conn, app_id) == "closed"
    app2 = ready(conn, FakeAdapter(submitter=stub), employer="Dutch Bank", sid="dutch/1")
    apps.queue_eligible(conn)
    _, cfg = policy.current(conn)
    cfg["excluded_areas"].append({"name": "Amsterdam", "country": "NL", "places": ["amsterdam"], "exceptions": []})
    policy.save(conn, cfg, "exclude Amsterdam")
    assert submission.submit_one(conn, app2, FakeAdapter(submitter=stub)) != "confirmed"
    assert state(conn, app2) == "awaiting_approval" and stub.calls == 0


def test_daily_limit(conn):
    seed_profile(conn); go_live(conn)
    _, cfg = policy.current(conn)
    cfg["limits"]["daily_submissions"] = 1
    policy.save(conn, cfg, "limit 1")
    ad = FakeAdapter(submitter=StubSubmitter())
    a = ready(conn, ad, employer="Bank A", sid="a/1")
    b = ready(conn, ad, employer="Bank B", sid="b/1")
    apps.queue_eligible(conn)
    assert submission.submit_one(conn, a, ad) == "confirmed"
    assert "daily submission limit" in submission.submit_one(conn, b, ad) and state(conn, b) == "queued"
