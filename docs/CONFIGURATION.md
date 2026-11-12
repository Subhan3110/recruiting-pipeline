# Configuration

## Environment variables
| Variable | Purpose |
|---|---|
| `JOBAGENT_DATABASE_URL` | `postgresql://...` for the shared database. Unset: SQLite at `~/.jobagent/jobagent.db` (single machine only) |
| `JOBAGENT_MASTER_KEY` | Fernet key encrypting secrets and documents in the database. Create with `python -m jobagent keygen` |
| `JOBAGENT_UI_TOKEN` | Optional token for the web interface when it is reachable beyond localhost |
| `JOBAGENT_CHROMIUM` | Optional Chromium path for Playwright |

## Shared memory for Claude Code, Codex and the worker
Git does not carry ignored files, so the private profile, documents, credentials and all history live in **one PostgreSQL database**.
Every runtime gets the same two secrets and nothing else:

1. Create a PostgreSQL database (any host; a free tier is enough for this volume). Require TLS (`?sslmode=require`).
2. Put `JOBAGENT_DATABASE_URL` and `JOBAGENT_MASTER_KEY` into each runtime's secret settings
   (Claude Code environment secrets, Codex environment secrets, the worker host). Never into the repository.
3. In each runtime run `python -m jobagent doctor`. It reports database type, migration count, a master-key round trip and how many
   stored secrets decrypt, without printing any value. Record the result in `docs/HANDOFF.md`.

Status: verified against a local PostgreSQL 16 only (full test suite). **No hosted shared database exists yet**; that needs the owner's choice of host.
The cloud environment must also allow the database host on its network allowlist.

## Secrets
`python -m jobagent secret-set <ref> --scope <mailbox|recruitment|api>` reads the value from stdin or a hidden prompt.
Scopes are enforced: a `mailbox` secret cannot be read for account creation.

| Reference | Scope | Use |
|---|---|---|
| `mail/gmail/client_id`, `mail/gmail/client_secret`, `mail/gmail/refresh_token` | mailbox | Gmail API, read-only scope |
| `mail/imap/password` | mailbox | IMAP provider |
| `recruitment/default` | recruitment | Optional password you supply for new portal accounts (otherwise unique generated ones) |
| `account/<id>` | recruitment | Generated per-account passwords (created by the agent) |

## Mailbox
Prefer a dedicated application mailbox or alias. Set `contact/application_email` in the profile to use it on forms.
- **Gmail**: `python -m jobagent set mail_provider '"gmail_api"'`. Needs an OAuth client and a refresh token for the single scope
  `gmail.readonly`. Gmail has no narrower scope (no per-sender or per-label access), which is the reason for a dedicated mailbox.
  The network allowlist must include `gmail.googleapis.com` and `oauth2.googleapis.com`.
- **IMAP**: `mail_provider "imap"`, plus `mail_imap_host`, `mail_imap_user`, `mail_imap_folder`. The folder is opened read-only.
  An app password gives the provider's full mailbox access, so use this for a dedicated mailbox.
Neither provider has been exercised against a real mailbox yet. The agent never sends, deletes or labels mail.

## Controls (`python -m jobagent set <key> true|false`, or the Overview page)
`discovery_enabled`, `preparation_enabled`, `submission_enabled`, `dry_run`, `global_pause`.
Live submission needs `global_pause=false`, `dry_run=false`, `submission_enabled=true`. The interface refuses to go live while
profile facts are unconfirmed.

## Policy
Edited as JSON on the Policy page; every save is a new version and each attempt records its version.
Key parts: `excluded_areas` (name, country, places, exceptions), `roles.min_score_auto`, `seniority`, `eligibility`,
`employers.max_auto_per_employer`, `limits`, `accounts`, `verification`, `salary`, `retention`. Defaults: `jobagent/policy.py`.

## Sources
`config/sources.toml` lists Greenhouse board tokens. Add a token after checking that
`https://boards-api.greenhouse.io/v1/boards/<token>/jobs` returns jobs.

## Backups, retention, deletion
- `python -m jobagent backup <file.json.gz>` writes an engine-neutral dump (secrets stay encrypted). `restore` loads it into an
  empty database. The round trip is tested. Backups contain personal data: keep them out of the repository.
- `backup.apply_retention` clears raw source payloads and old e-mail evidence per `policy.retention`.
- `backup.delete_personal_data(conn, "DELETE MY DATA")` erases profile, documents, answers, credentials and mail evidence.
- Accounts page: revoke a connection (deletes its stored credentials).

## Cloud network allowlist (environment `recruiting-pipeline`)
`boards-api.greenhouse.io`, `job-boards.greenhouse.io`, `boards.greenhouse.io`, `job-boards.cdn.greenhouse.io`,
`api-geocode-earth-proxy.greenhouse.io`, `www.recaptcha.net`, plus `pam-stilling-feed.nav.no`, `api.lever.co`, `api.ashbyhq.com` for planned adapters.
