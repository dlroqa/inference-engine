-- Block 9b: tamper-evident operator/security audit log.
-- Append-only. Each row carries a hash chain: hash = sha256(prev_hash + canonical
-- record), so any insertion, deletion, or edit of a past row breaks verification.
-- Details are structured metadata only — never prompts, responses, tokens, or key
-- secrets.

CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,           -- ISO-8601 UTC
    actor      TEXT NOT NULL,           -- key id, 'local', or 'cli'
    action     TEXT NOT NULL,           -- e.g. key.create, model.load
    target     TEXT,                    -- affected object id (key id, model id)
    detail     TEXT NOT NULL,           -- canonical JSON of non-secret metadata
    prev_hash  TEXT NOT NULL,           -- hash of the previous row (genesis = 64 zeros)
    hash       TEXT NOT NULL            -- sha256(prev_hash + canonical record)
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events (ts);
