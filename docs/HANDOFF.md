# Handoff

Last updated: 2026-11-21 (Claude Code, branch `claude/job-agent`, not merged to `main`, no pull request).

## Verified state
| Area | State | Evidence |
|---|---|---|
| Test suite | 74 tests pass on SQLite and on PostgreSQL 16 (local) | `python -m pytest -q`, `JOBAGENT_TEST_PG=...` |
| Greenhouse discovery | Live: 34 boards, 4,335 vacancies stored and evaluated | `jobs` rows all `done`; `python -m jobagent status` |
| Matching and geography | Live data after CV, policy v2 and work authorisation: 33 automatic-eligible, 417 held, 3,861 rejected | `evaluations` |
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
- Candidate profile: loaded from the owner's CV (2026-11-21), 37 facts, mostly confirmed. Still unconfirmed: current base (London inferred from the MSc),
  POWER job, Econa, Excel/VBA/financial-modelling skills, UK Graduate visa (future). Cover-letter paragraphs (`stories/*`) are **proposed**, so no cover letter is generated until approved.
- Work authorisation (2026-11-21): Norwegian passport. Confirmed: EEA states and Switzerland (no visa, sponsorship answered "No"). Liechtenstein is deliberately excluded
  (quota-limited permits). `work_auth/OTHER` (needs sponsorship everywhere else) is **unconfirmed** until the owner says Norwegian is their only citizenship; policy default is not to apply where sponsorship is needed.
  Postings that demand "EU citizenship" are held, because an EEA passport may not qualify.
- Live data now: 33 vacancies at 11 employers are automatic-eligible (EEA and Switzerland); 417 held; 3,861 rejected. A first dry-run preparation showed what forms ask:
  all 8 prepared applications stop at `needs_input` for standing answers (how heard, age 18, notice period, salary, location, school lists). 3 vacancies were already closed (form 404).

## Policy change 2026-11-21 (policy v2)
Owner: applications must be outside Norway and the UK. Implemented as two whole-country excluded areas next to the Oslo and London areas. Such roles stay discoverable and rankable
but need individual approval (assumption: the earlier approve-first rule still applies, rather than never applying). The owner expects a UK Graduate visa after the MSc (UK only, irrelevant to the automatic area).
Explicit state or country qualifiers now beat place names ("Manchester, NH" is not UK).

## Owner decisions 2026-11-21 (policy v3)
Norwegian is the only citizenship, no other permits. Apply everywhere including where sponsorship would be needed, but not where the posting rules sponsorship out (`eligibility.apply_when_sponsorship_needed=true`;
forms are answered truthfully: not authorised, sponsorship needed). Available from June 2027, willing to relocate, salary "in line with industry standards".
Standing answers stored as confirmed: start_date, salary_expectation, relocation, plus three chosen by Claude and disclosed to the owner: notice_period "None" (derived from the CV), age_18 "Yes", consent:privacy "Yes", heard_from "Company website".
Live state: 206 vacancies at 25 employers are automatic-eligible; 42 applications prepared against live forms stop at `needs_input`. The top gaps are `contact/location` (still unconfirmed, 17 forms),
politically-exposed-person / relative / referral questions, military service, years of experience, AI-tool consents and outstanding-offers. No form was submitted or dry-run with real data
(attaching a CV on a Greenhouse form uploads it to Greenhouse storage even without submitting, so live fills use the synthetic profile only).

## Update 2026-11-21 (evening)
Owner confirmed their current city and answered the compliance questions (details live only in the private database; this repository is public).
Prepared applications: 2 `queued` (Flow Traders Amsterdam internship, WorldQuant New York), 1 `awaiting_approval` (Ripple, London),
30 `needs_input` on employer-specific questions (AI-tool use, legal/preferred names, grades, subjects, office preferences), 10 `closed` (vacancy gone).
Everything is still dry run: nothing has been submitted. A first live submission is the next task and needs the owner's go-ahead.

## Shared database (2026-11-21)
Owner created a free Supabase project (West EU, Data API off). Cloud sessions cannot reach Postgres (HTTPS-only egress, DNS for database hosts does not resolve), so the agent will run on the owner's computer:
see `docs/LOCAL_SETUP.md`. `python -m jobagent backup --portable` + `restore` moves the data to a database with a new master key (verified into a local PostgreSQL). Done 2026-11-21 by a local Claude session on the owner's Mac (reported to this session with a screenshot): restore into Supabase
(4,335 vacancies, 21,662 evaluations, 143 employers, 190 applications, 71 profile facts; 33 unfinished applications sent back to preparation), `doctor` passes
(database, master key, profile), interface serving on the Mac. **Supabase is now the single source of truth.** The cloud container's SQLite copy is retired and paused.
Dry run no longer attaches documents on real employer forms (Greenhouse stores attachments on selection); the worker must `git pull` this change before it runs.

## Standing answers (2026-11-21, late)
The owner supplied answers for the remaining form questions (connections, military, licences, nationality, demographics, AI use). They live in a private answers file
imported with `python -m jobagent profile-import <file>` on the owner's Mac, never in this repository. Answer matching now handles ordered fallbacks (`A || B`),
self-identification questions by topic, visa-status and right-to-work option lists, office/relocation questions, date formats and season options, "Other" options,
and never writes em or en dashes in free text. Questions asking for longer text become `long_answer` tasks for the owner. Forms or descriptions that require a FINRA
licence close the application. Sandbox check against the live forms of the 33 unfinished applications: 1 left needing input (a flagged long answer), the rest prepared or queued.
Next on the Mac: `git pull`, `profile-import` the answers file, `python -m jobagent reprepare`.

## Mail (2026-11-21)
Owner chose the existing Gmail connector instead of a mailbox password. Added the `relay` provider, `mail-relay-needs` and `mail-relay-add`
(migration 002) and `docs/MAIL_RELAY.md`. Verified from the cloud session that the connector returns real Greenhouse acknowledgement mails and
that the acknowledgement wording matches the parser. Not yet run on the Mac against Supabase.

## Agents (2026-11-21)
The worker now runs as three roles (finder, writer, applier), see `docs/AGENTS_RUNTIME.md`. Cover letters are written by a Claude session
(`letters-needed`, `letter-add`), fact-checked, rendered to PDF by the writer role and attached by the applier. Verified on a sandbox copy: one letter
written, accepted by the checks and rendered (1 page); a fabricated percentage and an em dash are rejected in tests.

## Letter layout and logos (2026-11-21)
Cover letters now follow the owner's template (see `docs/AGENTS_RUNTIME.md`): dated the day they are made, employer logo, contact block, employer name,
salutation and sign-off added by the template, body written by the Claude writer in the owner's style (private fact `preferences/letter_example`).
Logos: Greenhouse board logo, then Wikipedia logo files, then the website icon; verified live on a sandbox for IMC, Point72, Adyen, Jane Street,
Flow Traders, Stripe, Brex and Monzo; Wikimedia rate-limits bursts, so requests are throttled. Vacancies whose stated graduation window
excludes the owner's graduation (Sep 2027) are now rejected.

## Blockers that need the owner
1. Where you live now (London?), and what your current visa allows while studying.
2. Compliance answers (PEP, relatives, referral, military, outstanding offers, AI-tool consent) and years of experience.
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
