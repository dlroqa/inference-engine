-- Block 3: API keys + per-key usage/attribution (also the quota counter source).

CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    key_hash     TEXT NOT NULL UNIQUE,   -- sha256 of the token; the token is never stored
    prefix       TEXT NOT NULL,          -- non-secret display prefix
    label        TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

-- One row per completed inference request. Doubles as the attribution log and
-- the source of truth for compute-unit (CU) quota windows.
CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,      -- unix epoch seconds (window math)
    key_id            TEXT,               -- NULL / 'local' for unauthenticated loopback
    request_id        TEXT NOT NULL,
    endpoint          TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cu                REAL NOT NULL DEFAULT 0,
    status            INTEGER
);

CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage_events(key_id, ts);
