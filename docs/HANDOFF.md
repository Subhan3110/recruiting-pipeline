# Handoff

Last updated: 2026-11-12 (Claude Code, branch `claude/job-agent`, not merged to `main`, no pull request).

## Verified state
| Area | State | Evidence |
|---|---|---|
| Test suite | 74 tests pass on SQLite and on PostgreSQL 16 (local) | `python -m pytest -q`, `JOBAGENT_TEST_PG=...` |
| Greenhouse discovery | Live: 34 boards, 4,335 vacancies stored and evaluated | `jobs` rows all `done`; `python -m jobagent status` |
| Matching and geography | Live data: 405 held, 3,930 rejected, 0 automatic (work authorisation is unconfirmed, so everything relevant holds) | `evaluations` |
| Form filling on a real Greenhouse form | Live dry run with the synthetic test profile: 10 fields incl. dropdowns, CV and letter upload, location. Submit not clicked | attempt outcome `dry_run`, `submit_clicked_at` NULL |
| Submit, confirmation, security code, ambiguous outcome, recovery | Real Chromium against the local fixture form only | `tests/test_submission.py` |
| Prior applications | 147 imported from the tracker artifact (63 active, 84 closed); 111 employers hold a used allowance | `applications.origin='imported'`, `employer_slots` |
| Web interface | All tabs render against the live-data database | manual check of HTTP 200 on every tab |
| Backup and restore | Round trip tested (SQLite to SQLite) | `test_backup_and_restore_roundtrip` |

## Not done or not verified
- **No real application has been submitted. No recruitment account has been created. No mailbox is connected.**
- Automatic submission is **off**: `dry_run=true`, `submission_enabled=false`.
- The real submit click, confirmation page, reCAPTCHA behaviour and security-code prompt are untested on a real employer form.
- Gmail API and IMAP providers are written but never run against a real mailbox. Their hosts are not on the network allowlist yet.
- Account creation/login is implemented as registry and credential logic with tests; no portal that needs an account (e.g. Workday) is automated.
- No hosted shared database. The session database was SQLite inside an ephemeral cloud container; a dump was handed to the owner.
- No always-on worker deployment. `python -m jobagent worker` works, but unattended hosting is not set up or verified.
- NAV Arbeidsplassen, Lever and Ashby adapters; model-assisted ranking and letter drafting; the tracker write-back (agent submissions into `pipeline.html` data).
- The environment setup script was saved but is unproven until a fresh session starts on the `recruiting-pipeline` environment.
- Candidate profile: 24 facts are **unconfirmed** (seeded from undated skill files, not from a CV). Open questions are in `user_tasks`.

## Blockers that need the owner
1. Current CV, and confirmation or correction of the seeded facts.
2. Citizenship and work authorisation per country, and whether to apply where sponsorship is needed.
3. Standing answers: availability, notice period, salary expectations, relocation, LinkedIn URL, demographic-question preference.
4. Application mailbox choice and read-only access.
5. Choice of PostgreSQL host for the shared database, then `JOBAGENT_DATABASE_URL` and `JOBAGENT_MASTER_KEY` as secrets in Claude Code and Codex.
6. Repository visibility: it is public. Code only is committed, but consider making it private or moving the agent to a private repository.

## How to run
See `README.md`. In a cloud session use the `recruiting-pipeline` environment.

## Next task
With the owner's answers: load the confirmed profile into the shared PostgreSQL database, re-import the tracker history, connect the
mailbox, then run one **watched** live submission to a single non-Oslo/London vacancy and record what the real post-submit screens look
like (update `browser/greenhouse_form.py` and `docs/SOURCES.md`). After that: Lever and Ashby adapters for broader coverage.
