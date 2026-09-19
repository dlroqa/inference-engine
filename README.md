# Inference Engine

A self-hosted, cross-platform inference engine. This repository is being built in
ordered **vertical slices** (Blocks 0–12) per
[`INFERENCE_ENGINE_CONSOLIDATED_DEPENDENCY_FLOW.md`](INFERENCE_ENGINE_CONSOLIDATED_DEPENDENCY_FLOW.md).
Each block must be deployable, testable, documented, and useful before the next
begins.

> **Current status: Block 1 — Local inference runtime.**
> On top of the Block 0 foundation, the engine now has a stable **internal
> generation contract** (`GenerationRequest` → async token stream →
> `GenerationResult`) and a local **llama.cpp** backend that serves one
> explicitly configured GGUF model. Blocking model work runs in a worker thread
> and streams tokens without blocking the event loop; load/unload state and
> cancellation are explicit. It is exercised via the `inference-engine generate`
> CLI and the internal API — **there is no HTTP inference API yet** (that is
> Block 2), and no authentication, dashboard, model downloads, multiple models,
> or GPU auto-tuning.

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

## Local inference (Block 1)

Install the optional llama.cpp backend (a native package) and point at a GGUF
model:

```bash
pip install -e ".[llama]"        # or: pip install "inference-engine[llama]"

inference-engine generate \
  --model-path /path/to/model.gguf \
  --prompt "The capital of France is" \
  --max-tokens 32
```

Tokens stream to stdout; a JSON result summary (finish reason, token counts,
TTFT/total ms) is printed to stderr. The model can also be set via
`IE_MODEL_PATH` or `model_path` in the config file.

**Internal generation API** (transport- and provider-agnostic — the seam future
HTTP dialects translate to):

```python
from engine.inference import GenerationRequest, build_backend
from engine.config import load_config

backend = build_backend(load_config())
await backend.load()
stream = backend.generate(GenerationRequest(prompt="Hello", max_tokens=32))
async for chunk in stream:
    print(chunk.text, end="")
result = stream.result  # GenerationResult: text, token counts, timings
await backend.unload()
```

Backend state is explicit (`unloaded → loading → ready → generating →
failed`), one model and one generation at a time. Closing a stream
(`await stream.aclose()`) cancels generation and releases the worker.

**Documented Block 1 limitations:** single resident model; generations are
serialized (a second concurrent request raises `BackendBusyError`); no
downloads, routing, GPU auto-tuning, batching, or HTTP inference API yet.
`llama-cpp-python` prebuilt wheels require AVX2; on CPUs without it, build from
source with AVX disabled.

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
`pyproject.toml`); the suite currently sits at ~94%.

### Continuous integration — the complete build/render/test pipeline

**GitHub Actions is the authoritative environment for all building, rendering,
and testing.** Local dev machines may lack the CPU features (AVX) or resources to
run the real inference or rendering paths soundly, so CI covers every need and is
the source of truth. Local commands below are for quick pre-push sanity only.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every
push/PR (and on demand via **workflow_dispatch**):

- **`test`** (Python 3.11 & 3.12): ruff lint, ruff format check, mypy, and
  pytest **with branch coverage** (fails under the 90% floor). Publishes a
  coverage table to the run summary and uploads `coverage.xml` + a JUnit report
  as artifacts.
- **`integration-llama`** (the AVX/real-inference environment): asserts the
  runner has **AVX2**, installs the real `llama-cpp-python` CPU backend,
  downloads + **checksum-verifies** a cached tiny GGUF, runs the gated
  integration test, and **renders a real completion** (uploaded as the
  `real-generation` artifact and shown in the run summary). This is the canonical
  place the llama.cpp path is exercised, since local dev CPUs may lack AVX. It
  also runs on demand via **workflow_dispatch**.
- **`build`**: builds the sdist + wheel with `python -m build`, installs the
  wheel into a clean virtualenv, and smoke-tests the packaged CLI
  (`inference-engine version` / `migrate`) so the distributable is verified — not
  just the source tree. Uploads the distributions as artifacts.
- **`ci-success`**: a single aggregator gate that passes only when **all** of the
  above jobs succeed (failing if any failed or was skipped). Use it as the one
  required status check for branch protection.

Future build/render/test needs are added here as jobs (e.g. real-SDK contract
tests against a live model, the React/Vite dashboard build + screenshots, Docker
image build, load tests) rather than run locally.

Build the distributables locally the same way (quick sanity only):

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
