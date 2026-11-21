# Run the agent on your own computer with the Supabase database

Why your computer: cloud Claude Code sessions only have HTTPS access, so they cannot open a Postgres connection. Your computer (or a small hosted worker) can.
The interface at `http://127.0.0.1:8787` is also only reachable from the machine that runs it.

## 1. One-time setup
Needs Python 3.11+ and git.
```bash
git clone https://github.com/Subhan3110/recruiting-pipeline.git
cd recruiting-pipeline
git checkout claude/job-agent
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[postgres]"
python -m playwright install chromium
```

## 2. Secrets (never in the repository, never in chat)
Supabase: Project → Connect (or Settings → Database) → **Session pooler** string, port 5432. Use the session pooler, not the transaction pooler: the agent relies on session behaviour.
Keep both values in a private file outside the repository, for example `~/.jobagent.env`:
```bash
export JOBAGENT_DATABASE_URL='postgresql://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres?sslmode=require'
export JOBAGENT_MASTER_KEY='<output of: python -m jobagent keygen>'
```
Then `source ~/.jobagent.env`. Store the master key in your password manager: losing it loses stored passwords and generated documents (documents can be rebuilt; stored account passwords cannot).

## 3. Create the schema and load your data
```bash
python -m jobagent init
python -m jobagent restore portable-2026-11-21.json.gz   # the file Claude sent you; refuses to run on a database that already has data
python -m jobagent doctor                                # database, master key, secrets, profile; prints no secret values
```
`restore` brings across your profile, answers, policy versions, vacancies, evaluations, employers, imported history and tasks.
It leaves out encrypted documents and secrets (they were encrypted with a key that only existed in the temporary cloud container), sends unfinished applications back to
preparation so the PDFs are rebuilt, and deletes old approvals. Delete the dump file afterwards: it contains personal data.

## 4. Use it
```bash
python -m jobagent serve              # interface: http://127.0.0.1:8787/?tab=actions
python -m jobagent worker             # discovery, preparation, dry-run submission loop (separate terminal)
```
New installs and restored data stay in **dry run** with live submission **off**. Nothing is sent until you turn dry run off and live submission on in the interface (Overview).

## 5. Afterwards
- Stop using the cloud container's SQLite file. The Supabase database is now the single source of truth.
- Supabase free projects pause after about a week without activity; a running worker keeps it awake.
- Claude Code cloud sessions still cannot reach Postgres. Run Claude Code (or Codex) on your computer with the same two variables for shared access, and record `python -m jobagent doctor` in `docs/HANDOFF.md`.
