-- Block 0 initial schema.
-- Deliberately minimal: only a key/value settings table for foundational
-- persistence. Domain tables (clients, api_keys, model_registry, usage_log, …)
-- are introduced by the blocks that own those features.

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
