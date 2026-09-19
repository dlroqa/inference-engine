# Inference Engine

A self-hosted, cross-platform inference engine. This repository is being built in
ordered **vertical slices** (Blocks 0–12) per
[`INFERENCE_ENGINE_CONSOLIDATED_DEPENDENCY_FLOW.md`](INFERENCE_ENGINE_CONSOLIDATED_DEPENDENCY_FLOW.md).
Each block must be deployable, testable, documented, and useful before the next
begins.

> **Current status: Block 0 — Foundation.**
> This is the application skeleton only. It starts, validates configuration,
> persists schema state (SQLite/WAL + migrations), logs structured JSON, and
> exposes health endpoints. **There is no inference, authentication, dashboard,
> remote binding, or model management yet** — those arrive in later blocks.

---

## Requirements

- Python **3.11+**
- Linux, macOS, or Windows (Block 0 is pure Python; CI tests Linux CPU first)

## Install (from a clone)

```bash
git clone <this-repo> "inference-engine"
cd "inference-engine"

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -e ".[dev]"           # runtime + dev tools (pytest, ruff, mypy)
```

## Configure

Configuration is layered. Highest precedence wins:

```
CLI flags  >  IE_* environment variables  >  config file (TOML)  >  defaults
```

- **Defaults:** loopback host `127.0.0.1`, port `8000`, log level `INFO`, and
  per-user OS data paths (via `platformdirs`).
- **Config file:** copy [`config.example.toml`](config.example.toml) and pass it
  with `--config ./config.toml`, or set `IE_CONFIG_FILE=/path/to/config.toml`.
- **Environment:** any setting via `IE_<NAME>`, e.g. `IE_PORT=9000`,
  `IE_LOG_LEVEL=DEBUG`.
- **CLI flags:** `--host`, `--port`, `--log-level`, `--data-dir`, `--db-path`,
  `--config`.

Invalid configuration fails immediately with a readable error (e.g. an
out-of-range port or an unknown key), rather than at first use.

Inspect the fully-resolved configuration:

```bash
inference-engine config
```

## Run

```bash
# Apply database migrations (also applied automatically on server startup)
inference-engine migrate

# Start the server
inference-engine serve --host 127.0.0.1 --port 8000
```

### Health endpoints

| Endpoint    | Meaning                                                                 |
|-------------|-------------------------------------------------------------------------|
| `GET /healthz` | Process **liveness**. `200` with `{"status":"ok", ...}` when running. |
| `GET /readyz`  | **Readiness** of foundational dependencies (database reachable, migrations applied). Returns `200` when ready, `503` otherwise. It **always reports `inference.available = false`** — inference does not exist in this build. |

Example:

```bash
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok","version":"0.0.0"}

curl -s http://127.0.0.1:8000/readyz
# {"status":"ready","version":"0.0.0","checks":{"database":"ok","migrations":"applied"},
#  "inference":{"available":false,"reason":"inference runtime not implemented in this build (Block 0 foundation)"}}
```

## Test & checks

```bash
ruff check .                                   # lint
ruff format --check .                           # formatting
mypy                                            # type check
pytest --cov=engine --cov-report=term-missing   # tests + coverage
```

Coverage is enforced with a floor of **90%** (see `[tool.coverage.report]` in
`pyproject.toml`); the suite currently sits at ~98%.

### Continuous integration — build solidification

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) measures the build on
every push/PR:

- **`test`** (Python 3.11 & 3.12): ruff lint, ruff format check, mypy, and
  pytest **with branch coverage** (fails under the 90% floor). Publishes a
  coverage table to the run summary and uploads `coverage.xml` + a JUnit report
  as artifacts.
- **`build`**: builds the sdist + wheel with `python -m build`, installs the
  wheel into a clean virtualenv, and smoke-tests the packaged CLI
  (`inference-engine version` / `migrate`) so the distributable is verified — not
  just the source tree. Uploads the distributions as artifacts.

Build the distributables locally the same way:

```bash
python -m build            # -> dist/*.whl, dist/*.tar.gz
```

## Data & storage

- **Database:** SQLite in **WAL** mode, at `db_path` (defaults to
  `<data_dir>/inference_engine.db`).
- **Migrations:** ordered `engine/store/migrations/NNNN_*.sql` files, applied
  once each and tracked in a `schema_migrations` table. Idempotent and applied
  on startup. (A heavier Alembic/Postgres path is planned for later blocks.)
- All storage/config/log paths are overridable via config.

## Project layout (Block 0)

```
engine/
├── main.py             # FastAPI app factory + lifespan (logging, migrations)
├── config.py           # layered pydantic-settings configuration
├── cli.py              # `inference-engine` CLI (serve/migrate/config/version)
├── logging_setup.py    # structured JSON logging
├── api/health.py       # /healthz, /readyz
└── store/
    ├── db.py           # SQLite (WAL) connection
    ├── migrations.py   # migration runner
    └── migrations/     # NNNN_*.sql migration files
tests/                  # pytest suite
```

## License

Proprietary. All rights reserved.
