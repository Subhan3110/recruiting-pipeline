# Recruiting Pipeline

Two things live here:

- **Job agent** (`jobagent/`): finds finance vacancies, matches them against a confirmed candidate profile, prepares applications and
  submits them under a versioned standing policy. Oslo and London applications always wait for individual approval.
- **Tracker** (`pipeline.html`): the existing recruiting tracker artifact. See [docs/TRACKER.md](docs/TRACKER.md).

This repository is public. All personal data, documents and credentials live in the database, never in Git.

## Quick start
```bash
pip install -e ".[postgres,dev]"
python -m jobagent keygen            # once; store the output as the JOBAGENT_MASTER_KEY secret
export JOBAGENT_MASTER_KEY=...       # and JOBAGENT_DATABASE_URL=postgresql://... for the shared database
python -m jobagent init
python -m jobagent doctor            # connection checks, prints no secrets
python -m jobagent profile-import /private/path/profile.json   # shape: config/profile.example.json
python -m jobagent run-once          # discover, evaluate, prepare, (dry-run) submit
python -m jobagent serve             # interface on http://127.0.0.1:8787
python -m jobagent worker            # continuous background worker
```
New installs start in **dry run** with live submission **off**. Dry run fills forms and never clicks submit.

## Going live (checklist)
1. Confirm profile facts in the interface (Profile) and record work authorisation per country.
2. Answer the open questions (Actions). Answers are stored in the reusable answer library.
3. Import earlier applications (Import) so they count toward duplicate and one-per-employer rules.
4. Connect a mailbox (see [docs/CONFIGURATION.md](docs/CONFIGURATION.md)) for verification codes and acknowledgements.
5. Review the policy (Policy). Then turn dry run off and live submission on (Overview). The global pause stops everything at once.

## Documentation
- [AGENTS.md](AGENTS.md): rules for Claude Code and Codex
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, state machine, invariants
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): database, secrets, mailbox, policy, backups, shared access
- [docs/SOURCES.md](docs/SOURCES.md): source adapters and what each can really do
- [docs/HANDOFF.md](docs/HANDOFF.md): current verified state and next task

## Tests
```bash
python -m pytest -q
JOBAGENT_TEST_PG=postgresql://user@host/db python -m pytest -q   # same suite on PostgreSQL
```
Submission tests drive real Chromium against a local stand-in form (`tests/fixtures/form_server.py`).
