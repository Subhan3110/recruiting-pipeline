-- Employer logos for cover letters. Public images, re-encoded to PNG before storage.
CREATE TABLE logos (
  employer_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN ('ok','none','manual')),
  png_b64 TEXT, source_url TEXT, width INTEGER, height INTEGER, checked_at TEXT NOT NULL
);
