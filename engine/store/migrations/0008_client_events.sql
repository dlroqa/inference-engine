-- Block 11.5: a durable, ordered, per-client event log backing client SSE.
--
-- The autoincrement id is the stream cursor (the SSE "id:" / Last-Event-ID), and
-- per-client ordering is (client_id, id). UNIQUE(client_id, event_id) makes
-- emission idempotent: re-emitting the same logical event for a client is a
-- no-op, so a reconnecting client never sees duplicates. Rows hold structured
-- metadata only — never prompts, responses, or secrets.

CREATE TABLE IF NOT EXISTS client_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  TEXT NOT NULL REFERENCES clients(id),
    event_id   TEXT NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,   -- the JSON event envelope as delivered
    ts         REAL NOT NULL,
    UNIQUE (client_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_client_events_stream ON client_events(client_id, id);
