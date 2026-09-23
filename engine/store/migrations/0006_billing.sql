-- Block 11: clients own API keys; plans define entitlements; a single billing
-- provider (Stripe) drives the lifecycle. Inbound webhooks are idempotent.

-- Entitlement definitions. Operator-controlled; never written from webhook
-- content. quota_*_cu of 0 means unlimited; NULL rate_limit means "use the
-- engine-wide default"; NULL allowed_models means "all models".
CREATE TABLE IF NOT EXISTS plans (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    quota_5h_cu        REAL NOT NULL DEFAULT 0,
    quota_weekly_cu    REAL NOT NULL DEFAULT 0,
    rate_limit_per_min INTEGER,
    allowed_models     TEXT,               -- JSON array of model ids, or NULL
    created_at         TEXT NOT NULL
);

-- A commercial client: the owner of one or more API keys. external_ref is the
-- billing provider's customer id (e.g. Stripe "cus_..."), used to correlate
-- inbound webhooks to a client without trusting the payload for entitlements.
CREATE TABLE IF NOT EXISTS clients (
    id           TEXT PRIMARY KEY,
    external_ref TEXT UNIQUE,
    email        TEXT,
    status       TEXT NOT NULL DEFAULT 'active',  -- active | suspended | canceled
    created_at   TEXT NOT NULL
);

-- One subscription per provider subscription id. Its status and plan are the
-- current entitlement source for the owning client.
CREATE TABLE IF NOT EXISTS subscriptions (
    id                 TEXT PRIMARY KEY,
    client_id          TEXT NOT NULL REFERENCES clients(id),
    plan_id            TEXT REFERENCES plans(id),
    provider           TEXT NOT NULL,       -- 'stripe'
    provider_sub_id    TEXT NOT NULL,
    status             TEXT NOT NULL,       -- active | past_due | canceled
    current_period_end TEXT,
    updated_at         TEXT NOT NULL,
    UNIQUE (provider, provider_sub_id)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_client ON subscriptions(client_id);

-- Inbound-webhook idempotency ledger: one row per accepted provider event id.
-- A duplicate delivery finds its row and is a no-op.
CREATE TABLE IF NOT EXISTS webhook_events (
    provider     TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    event_type   TEXT,
    received_at  TEXT NOT NULL,
    processed_at TEXT,
    result       TEXT,
    PRIMARY KEY (provider, event_id)
);

-- Keys gain an optional owning client and an enforcement status. Existing keys
-- default to unowned + active, so Block 3 behavior is unchanged: an unowned key
-- keeps using the engine-wide quota limits.
ALTER TABLE api_keys ADD COLUMN client_id TEXT REFERENCES clients(id);
ALTER TABLE api_keys ADD COLUMN status TEXT NOT NULL DEFAULT 'active';  -- active | suspended | revoked

CREATE INDEX IF NOT EXISTS idx_api_keys_client ON api_keys(client_id);
