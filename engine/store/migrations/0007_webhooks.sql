-- Block 11.2: outbound Standard Webhooks. Clients register endpoints; the engine
-- signs and delivers events with retries, a dead-letter state, and a replayable
-- delivery log. Secrets are multi-row so they can be rotated with a grace window.

-- A client-owned destination for outbound events. event_types is a JSON array of
-- subscribed event types, or NULL for "all".
CREATE TABLE IF NOT EXISTS webhook_endpoints (
    id          TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL REFERENCES clients(id),
    url         TEXT NOT NULL,
    description TEXT,
    disabled    INTEGER NOT NULL DEFAULT 0,
    event_types TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_client ON webhook_endpoints(client_id);

-- Signing secrets. Multiple active (non-expired) secrets let an endpoint rotate:
-- deliveries are signed with every active secret so a receiver verifying with the
-- old or the new secret both succeed until the old one expires.
CREATE TABLE IF NOT EXISTS webhook_secrets (
    id          TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
    secret      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  REAL              -- unix seconds; NULL = no expiry (current secret)
);

CREATE INDEX IF NOT EXISTS idx_webhook_secrets_endpoint ON webhook_secrets(endpoint_id);

-- One row per (endpoint, event). The delivery log and the work queue in one table.
-- UNIQUE(endpoint_id, event_id) makes fan-out and re-emission idempotent.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id               TEXT PRIMARY KEY,
    endpoint_id      TEXT NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
    event_id         TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    payload          TEXT NOT NULL,      -- the JSON body as sent
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending|succeeded|failed|dead
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL,      -- unix seconds; due when <= now and pending
    last_status_code INTEGER,
    last_error       TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    UNIQUE (endpoint_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
    ON webhook_deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint
    ON webhook_deliveries(endpoint_id, created_at);
