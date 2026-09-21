-- Block 6: the model registry — one row per imported/downloaded GGUF model.
-- The registry is the source of truth for which models exist, their verified
-- checksums, probed metadata, download progress, and which one is active.

CREATE TABLE IF NOT EXISTS models (
    id                TEXT PRIMARY KEY,        -- uuid hex
    name              TEXT NOT NULL,           -- display name / served model id
    filename          TEXT NOT NULL,           -- basename on disk
    path              TEXT NOT NULL,           -- absolute path (under models dir, or imported in place)
    source_type       TEXT NOT NULL,           -- 'huggingface' | 'url' | 'import'
    source_ref        TEXT,                    -- repo/file, url, or original import path
    sha256            TEXT,                    -- verified checksum (NULL if unknown/unverified)
    expected_sha256   TEXT,                    -- checksum to verify a download against
    size_bytes        INTEGER,                 -- total size (known for downloads once headers arrive)
    downloaded_bytes  INTEGER NOT NULL DEFAULT 0,
    quant             TEXT,                    -- GGUF file type / quant (e.g. Q4_K_M)
    arch              TEXT,                    -- GGUF general.architecture (e.g. llama)
    context_length    INTEGER,                 -- GGUF metadata if present
    status            TEXT NOT NULL,           -- downloading|verifying|ready|error|cancelled
    error             TEXT,                    -- last error detail (redacted, no secrets)
    active            INTEGER NOT NULL DEFAULT 0,  -- at most one active model
    added_at          TEXT NOT NULL,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_models_status ON models(status);
-- At most one active model at a time (partial unique index over active=1).
CREATE UNIQUE INDEX IF NOT EXISTS idx_models_one_active ON models(active) WHERE active = 1;
