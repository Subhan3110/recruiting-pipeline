-- Messages relayed from a mail connector by a Claude session (for example the Gmail connector).
-- Only recruitment messages are relayed; rows are short-lived (see backup.apply_retention).
CREATE TABLE mail_relay (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, message_ref TEXT NOT NULL, received_at TEXT NOT NULL,
  sender TEXT NOT NULL, recipients TEXT NOT NULL, subject TEXT, text TEXT, created_at TEXT NOT NULL,
  UNIQUE (provider, message_ref)
);
CREATE INDEX mail_relay_received ON mail_relay(received_at);
