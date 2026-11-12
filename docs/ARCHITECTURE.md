# Architecture

Plain Python, one database, one worker loop, one small web app. No ORM, no message broker: the database is the queue,
the journal and the memory, so any worker (or agent) can stop and another can continue.

```
sources/ (adapters) -> vacancies -> matching -> applications.prepare -> queue -> submission -> evidence
                          ^            ^               ^                              ^
                      employers     policy         answers, documents          mail/verification
                                    profile (facts, comments, tasks)           accounts, secretstore
```

| Module | Responsibility |
|---|---|
| `db.py` | Connection, portable SQL, transactions with a named cross-worker lock, migrations, settings, audit `events` |
| `profile.py` | Candidate facts with status, source, timestamps and supersede history; verbatim comments; user tasks |
| `policy.py`, `geo.py` | Versioned standing policy; location classification and the Oslo/London boundary |
| `employers.py`, `vacancies.py` | Employer identity and aliases; vacancy storage, cross-source dedup, repost detection |
| `sources/` | Adapter interface with declared capabilities; Greenhouse adapter |
| `matching.py` | Hard checks (pass / fail / unknown) plus an explainable priority score |
| `answers.py` | Scoped answer library; turns a form schema into concrete answers or explicit gaps |
| `documents.py` | CV and cover letter from confirmed facts; render check; encrypted immutable storage |
| `applications.py` | State machine, versions, approvals, import of earlier applications, per-employer selection |
| `submission.py` | Final gate, claim, attempt journal, outcomes, crash recovery, reconciliation |
| `browser/` | Playwright form automation |
| `mail/`, `verification.py` | Read-only mailbox providers; code/link matching; acknowledgement reconciliation |
| `accounts.py`, `secretstore.py` | Portal account metadata; Fernet-encrypted secrets with scopes |
| `jobqueue.py`, `pipeline.py` | Durable jobs with backoff; idempotent stages; `tick()` |
| `web.py`, `cli.py`, `backup.py` | Interface, commands, dump/restore, retention, deletion, revocation |

## Application states
`evaluated -> prepared -> queued -> submitting -> submitted`, with side exits
`awaiting_approval` (Oslo/London, or multi-location without a selectable eligible location),
`needs_input` (missing answer, CAPTCHA, verification code), `uncertain` (click journalled, no confirmation),
`failed`, `closed`. Every change writes a `transitions` row (time, reason, actor). Allowed moves: `applications.ALLOWED`.

## Invariants (tested)
1. **One application per vacancy**: `applications.vacancy_id` is UNIQUE; retries are `attempts` under the same row.
2. **One automatic application per employer**: `employer_slots` PRIMARY KEY `(employer_id, slot)`. A slot is taken inside the claim
   transaction and released only when the portal demonstrably received nothing. Imported history takes slots too.
   An individually approved application is an explicit decision and may take an extra slot.
3. **Approval is bound to content**: `approvals.content_hash` must equal the hash of the current version (answers, document hashes,
   selected location). Any change revokes it and returns the application to `awaiting_approval`.
4. **Gate and claim are atomic**: `submission.claim` runs under `tx(lock="submission")` (SQLite `BEGIN IMMEDIATE`, PostgreSQL
   `pg_advisory_xact_lock`) and re-checks pause, mode, policy version, exclusion, approval, unresolved attempts, limits and the slot.
5. **Re-check on live data first**: `submission.refresh` re-reads the vacancy (still open?), re-evaluates under the current policy and
   re-prepares answers from the live form before every attempt.
6. **Click journal**: `Hooks.before_submit` re-checks the pause and writes `attempts.submit_clicked_at` *before* the click.
   A failure after that is `uncertain` unless the portal itself showed that nothing was sent (form errors, code prompt, CAPTCHA).
7. **Uncertain blocks**: no new attempt for that vacancy, and the employer slot stays taken, until an acknowledgement e-mail or the
   user settles it (`submission.resolve_uncertain`).
8. **Restart recovery**: `submission.recover` resolves stale `submitting` rows from the journal (clicked -> `uncertain`, not clicked -> `queued`).
9. **Unknown is not no**: work authorisation, location or employer identity that is not confirmed produces `hold`, never a guess.
10. **Only confirmed facts leave the system**; each version stores the fact snapshot used, so later corrections never rewrite history.

## Untrusted input
Vacancy HTML is stripped to text before storage. Matching is regex and arithmetic over that text: there is nothing in it that can
change settings, policy or approvals. E-mails are only read for a pending `verification_requests` row (recipient, sender domain,
time window, employer hint) and only a code or an on-portal https link is extracted. Links are followed hop by hop and refused the
moment a hop leaves the portal's hosts. The browser refuses form URLs outside the adapter's `allowed_hosts`.

## Model assistance
Not implemented. `policy.model.provider` is `"none"` and `model_usage` exists for a budget ledger. Matching and letters are
deterministic today; a model may later rank or draft, but hard constraints and facts stay rule-based. Adding a paid provider needs the owner's approval.
