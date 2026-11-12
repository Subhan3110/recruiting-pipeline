-- Portable DDL: runs unchanged on SQLite (>=3.35) and PostgreSQL.
-- Conventions: TEXT ids, ISO-8601 UTC TEXT timestamps, JSON as TEXT, booleans as INTEGER 0/1.

CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  entity_type TEXT, entity_id TEXT, data TEXT, provenance TEXT
);
CREATE INDEX events_entity ON events(entity_type, entity_id);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE policy_versions (
  version INTEGER PRIMARY KEY, created_at TEXT NOT NULL, config TEXT NOT NULL, note TEXT, actor TEXT NOT NULL
);

-- Candidate memory -----------------------------------------------------------
CREATE TABLE profile_facts (
  id TEXT PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('confirmed','unconfirmed','preference','proposed')),
  source TEXT NOT NULL, confirmed_at TEXT, note TEXT,
  created_at TEXT NOT NULL, superseded_at TEXT, superseded_by TEXT
);
CREATE UNIQUE INDEX profile_facts_current ON profile_facts(key) WHERE superseded_at IS NULL;

CREATE TABLE profile_comments (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, verbatim TEXT NOT NULL, interpretation TEXT
);

CREATE TABLE answers (
  id TEXT PRIMARY KEY, question_key TEXT NOT NULL, scope_type TEXT NOT NULL CHECK (scope_type IN ('global','country','employer')),
  scope_value TEXT NOT NULL DEFAULT '', answer TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('confirmed','proposed')), source TEXT NOT NULL,
  confirmed_at TEXT, created_at TEXT NOT NULL, superseded_at TEXT
);
CREATE UNIQUE INDEX answers_current ON answers(question_key, scope_type, scope_value) WHERE superseded_at IS NULL;

CREATE TABLE user_tasks (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT,
  entity_type TEXT, entity_id TEXT, dedup_key TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dismissed')),
  resolution TEXT, resolved_at TEXT
);

-- Employers and vacancies ----------------------------------------------------
CREATE TABLE employers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT, kind TEXT NOT NULL DEFAULT 'employer',
  parent_id TEXT, excluded INTEGER NOT NULL DEFAULT 0, exclusion_reason TEXT,
  extra_auto_allowed INTEGER NOT NULL DEFAULT 0, identity_uncertain INTEGER NOT NULL DEFAULT 0,
  notes TEXT, created_at TEXT NOT NULL
);
CREATE TABLE employer_aliases (
  id TEXT PRIMARY KEY, employer_id TEXT NOT NULL REFERENCES employers(id),
  alias_type TEXT NOT NULL CHECK (alias_type IN ('name','domain','source')), alias TEXT NOT NULL,
  provenance TEXT, created_at TEXT NOT NULL, UNIQUE (alias_type, alias)
);

CREATE TABLE vacancies (
  id TEXT PRIMARY KEY, employer_id TEXT NOT NULL REFERENCES employers(id), title TEXT NOT NULL,
  canonical_url TEXT, description TEXT, requirements TEXT, location_raw TEXT, locations TEXT,
  language TEXT, deadline TEXT, salary TEXT, fingerprint TEXT NOT NULL,
  duplicate_of TEXT, needs_reconciliation INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  dismissed_at TEXT, dismissed_reason TEXT,
  first_seen TEXT NOT NULL, last_checked TEXT NOT NULL, closed_at TEXT
);
CREATE INDEX vacancies_fp ON vacancies(employer_id, fingerprint);
CREATE TABLE vacancy_sources (
  id TEXT PRIMARY KEY, vacancy_id TEXT NOT NULL REFERENCES vacancies(id), source TEXT NOT NULL,
  source_id TEXT NOT NULL, url TEXT, raw TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  UNIQUE (source, source_id)
);
CREATE TABLE evaluations (
  id TEXT PRIMARY KEY, vacancy_id TEXT NOT NULL REFERENCES vacancies(id), created_at TEXT NOT NULL,
  policy_version INTEGER NOT NULL, score REAL NOT NULL, decision TEXT NOT NULL, geo TEXT NOT NULL,
  rationale TEXT NOT NULL, detail TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX evaluations_current ON evaluations(vacancy_id) WHERE is_current = 1;

-- Documents, accounts, secrets ----------------------------------------------
CREATE TABLE documents (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL,
  content_enc TEXT NOT NULL, meta TEXT, created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE secrets (ref TEXT PRIMARY KEY, scope TEXT NOT NULL, ciphertext TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE accounts (
  id TEXT PRIMARY KEY, portal_origin TEXT NOT NULL, tenant TEXT NOT NULL DEFAULT '', employer_id TEXT,
  login_identifier TEXT NOT NULL, credential_ref TEXT, credential_source TEXT,
  verification_status TEXT NOT NULL DEFAULT 'unverified', last_login_ok TEXT, failed_logins INTEGER NOT NULL DEFAULT 0,
  pending_action TEXT, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (portal_origin, tenant, login_identifier)
);

-- Applications ---------------------------------------------------------------
CREATE TABLE applications (
  id TEXT PRIMARY KEY, vacancy_id TEXT UNIQUE REFERENCES vacancies(id), employer_id TEXT NOT NULL REFERENCES employers(id),
  origin TEXT NOT NULL CHECK (origin IN ('agent','imported','manual')),
  state TEXT NOT NULL CHECK (state IN ('discovered','evaluated','prepared','awaiting_approval','queued','submitting','submitted','needs_input','uncertain','failed','closed')),
  state_reason TEXT, requires_approval INTEGER NOT NULL DEFAULT 0, selected_location TEXT,
  current_version INTEGER, policy_version INTEGER, account_id TEXT,
  claimed_by TEXT, claimed_at TEXT, role_title TEXT, external_ref TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT
);
CREATE INDEX applications_employer ON applications(employer_id, state);
CREATE TABLE application_versions (
  id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id), version INTEGER NOT NULL,
  content_hash TEXT NOT NULL, answers TEXT NOT NULL, document_ids TEXT NOT NULL, facts_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE (application_id, version)
);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id), content_hash TEXT NOT NULL,
  approved_at TEXT NOT NULL, actor TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE transitions (
  id TEXT PRIMARY KEY, application_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
  ts TEXT NOT NULL, reason TEXT, actor TEXT NOT NULL
);
CREATE TABLE attempts (
  id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id), version INTEGER NOT NULL,
  policy_version INTEGER NOT NULL, worker TEXT NOT NULL, mode TEXT NOT NULL, started_at TEXT NOT NULL,
  submit_clicked_at TEXT, finished_at TEXT,
  outcome TEXT CHECK (outcome IN ('confirmed','uncertain','not_submitted','blocked','dry_run','aborted')),
  evidence TEXT, error TEXT
);
-- One slot per permitted application at an employer; the UNIQUE key is the cross-worker guard.
CREATE TABLE employer_slots (
  employer_id TEXT NOT NULL, slot INTEGER NOT NULL, application_id TEXT NOT NULL UNIQUE, taken_at TEXT NOT NULL,
  PRIMARY KEY (employer_id, slot)
);

-- Email ----------------------------------------------------------------------
CREATE TABLE verification_requests (
  id TEXT PRIMARY KEY, purpose TEXT NOT NULL, portal_origin TEXT NOT NULL, employer_hint TEXT, recipient TEXT NOT NULL,
  application_id TEXT, account_id TEXT, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  resend_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','consumed','expired','failed')),
  message_ref TEXT, resolved_at TEXT
);
CREATE TABLE email_messages (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, message_ref TEXT NOT NULL, received_at TEXT NOT NULL,
  sender_domain TEXT, kind TEXT NOT NULL, application_id TEXT, verification_request_id TEXT,
  evidence TEXT, created_at TEXT NOT NULL, UNIQUE (provider, message_ref)
);

-- Durable queue and model usage ---------------------------------------------
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, dedup_key TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
  run_after TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
  locked_by TEXT, locked_at TEXT, last_error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE UNIQUE INDEX jobs_dedup ON jobs(dedup_key) WHERE status IN ('queued','running');
CREATE TABLE model_usage (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL NOT NULL
);
