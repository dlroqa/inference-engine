-- Block 4: bounded mirror of recent structured log events for the Logs view and
-- diagnostics export. This is an operability aid, not the primary log sink (that
-- is the rotating JSON stream); the app trims it to a bounded row count.
--
-- By construction it stores only structured metadata (level, request id, route,
-- model, key id, error category, stage, message, stacktrace) and never raw
-- prompts, responses, or secrets.

CREATE TABLE IF NOT EXISTS log_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,          -- unix epoch seconds
    level       TEXT NOT NULL,          -- CRITICAL/ERROR/WARNING/INFO/DEBUG
    logger      TEXT,
    event       TEXT NOT NULL,          -- short event name / message
    request_id  TEXT,
    route       TEXT,
    model       TEXT,
    key_id      TEXT,
    category    TEXT,                   -- error taxonomy (see telemetry/taxonomy.py)
    stage       TEXT,                   -- where in the request lifecycle it happened
    detail      TEXT,                   -- redacted message detail (no prompts/secrets)
    stacktrace  TEXT                    -- present on failures
);

CREATE INDEX IF NOT EXISTS idx_log_events_ts ON log_events(ts);
CREATE INDEX IF NOT EXISTS idx_log_events_request ON log_events(request_id);
