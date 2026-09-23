# Inference Engine

A self-hosted, cross-platform inference engine. This repository is being built in
ordered **vertical slices** (Blocks 0–12) per
[`INFERENCE_ENGINE_CONSOLIDATED_DEPENDENCY_FLOW.md`](INFERENCE_ENGINE_CONSOLIDATED_DEPENDENCY_FLOW.md).
Each block must be deployable, testable, documented, and useful before the next
begins.

> **Current status: Block 10 — Remote scale-out (sub-slices 1–5 + 7: adapters, registry, affinity, auto-models, cost metrics, external spillover).**
> Shipped so far: local GGUF inference (Block 1); **OpenAI** `/v1/chat/completions`
> + **Anthropic** `/v1/messages` with streaming, sampling controls, and
> capability-gated structured output (Blocks 2, 8); a secure gateway — API keys,
> request/rate/concurrency limits, and a compute-unit quota (Block 3); operability
> telemetry + an operator dashboard (Blocks 4–5); model lifecycle with
> checksum-verified downloads (Block 6); controlled concurrency with admission
> control (Block 7); and a Docker deployment path with graceful drain, readiness
> gating, backup/restore (9a), and a security hardening baseline — trusted-network/IP policy, egress + feature kill switches, and a tamper-evident audit log (9b); and **remote vLLM + SGLang backends** — proxy generation to an external OpenAI-compatible server (one shared adapter core) with explicit model mapping, secure credentials, timeouts, and safe pre-stream-only failover, plus a **backend registry** that routes each request to the least-busy healthy backend across a local+remote pool with a status API (Block 10, sub-slices 1–3). **Not yet:** the rest of remote scale-out (Triton, KV/prefix affinity, routing policies + auto-models — Block 10), organizations/billing
> (Block 11), and advanced safety tooling (Block 12).

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

Backend limitations: single resident model; generations are serialized (a second
concurrent request is rejected as busy); no downloads, routing, GPU auto-tuning,
or batching yet. `llama-cpp-python` prebuilt wheels require AVX2; on CPUs without
it, build from source with AVX disabled.

## Remote backends: vLLM & SGLang (Block 10)

Instead of the local llama.cpp runtime, the engine can proxy generation to an
external **vLLM** or **SGLang** server over its OpenAI-compatible API, while still
owning auth, limits, quotas, admission control, telemetry, and both provider
edges. Both adapters share one core, so they behave identically — only
`backend_kind` differs. Install the `remote` extra and select the backend:

```bash
pip install "inference-engine[remote]"
```

```toml
backend_kind    = "remote_vllm"                          # or "remote_sglang"
model_id        = "team-llama3"                          # what clients request
remote_base_url = "http://vllm.internal:8000/v1"
remote_model    = "meta-llama/Meta-Llama-3-8B-Instruct"  # what the server serves
```

The API key (`IE_REMOTE_API_KEY`) is sent only to the remote and never logged or
echoed. On load the backend verifies via `/v1/models` that the remote actually
serves `remote_model`. Connect/read timeouts are bounded. A transient failure
**before the first token** is retried up to `remote_max_prestream_retries`; once
any token has reached the client an upstream error ends the stream honestly —
never a silent re-route. Set `allow_remote_backends = false` to refuse outbound
inference. Structured output on remote, **Triton** (deferred until a real multi-model
workload justifies it), and virtual auto-models are later Block 10 sub-slices. See
[docs/backends.md](docs/backends.md).

**Multiple backends & routing (sub-slices 3–4):** add `[[remote_workers]]` to run
several backends (local + remote) at once; the engine routes each request to the
least-busy healthy one and exposes the pool via `GET /admin/backends`. Where a
backend keeps a prefix cache (vLLM/SGLang, not llama.cpp), `prefix_affinity_chars`
routes prompts sharing a prefix to the same worker, and the status endpoint
surfaces its KV-cache utilization and prefix-cache hit rate — capability-gated, so
llama.cpp reports neither. **Virtual auto-models** (`[[virtual_models]]`) map a
client-facing model name to a **route** or **cascade** policy over the pool, with
selection-time fallback and a `POST /admin/route/plan` dry-run that explains the
decision without generating. `GET /admin/routes` attributes token **cost** and
latency/success signals per `(model, backend)` (measurement only). **External
providers** (`[[external_providers]]`) can serve as **spillover** when the local
pool is saturated — off by default, egress-allowlisted, secret-safe, metered, and
chosen only at admission (never a re-route after tokens are sent). See
[docs/backends.md](docs/backends.md).

## OpenAI-compatible API (Block 2)

Start the server with a model configured, then point any OpenAI client at it:

```bash
IE_MODEL_PATH=/path/to/model.gguf IE_MODEL_ID=my-model \
  inference-engine serve --host 127.0.0.1 --port 8000
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-checked-yet")

# non-streaming
r = client.chat.completions.create(
    model="my-model",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(r.choices[0].message.content)

# streaming
for chunk in client.chat.completions.create(
    model="my-model",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

Supported surface and error contract are documented in the
[compatibility matrix](docs/compatibility.md).

## Anthropic-compatible API (Block 8)

The engine also speaks the **Anthropic Messages** dialect at `POST /v1/messages`,
so the official `anthropic` SDK works drop-in:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8000", api_key="sk-ie-…")
msg = client.messages.create(
    model="my-model",  # the loaded model id
    max_tokens=128,
    system="You are concise.",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(msg.content[0].text)
```

Streaming emits the documented Anthropic event sequence (`message_start` →
`content_block_start` → `content_block_delta*` → `content_block_stop` →
`message_delta` → `message_stop`). Text content only; tool use and non-text
content blocks are rejected with a clear `400`. See the
[compatibility matrix](docs/compatibility.md).

### Sampling & structured output (Block 8)

Both dialects map onto one internal sampler set with a **documented, fixed order**
(repetition penalties → top_k → top_p → min_p → temperature; Mirostat replaces the
truncation step when enabled). OpenAI contributes `frequency_penalty` /
`presence_penalty`; Anthropic contributes `top_k`; `min_p`, `repeat_penalty`, and
Mirostat are available on the internal contract.

**Structured output** is honored when the loaded backend supports constrained
decoding (the `llama-cpp-python` backend does): OpenAI `response_format`
`json_object` / `json_schema` constrain the output, and a GBNF `grammar` is
supported internally. When the backend cannot constrain output, a JSON
`response_format` is **rejected** (`400 structured_output_unsupported`) rather than
silently returned as free text. Schema conformance is verified against a real
model in CI.

### Authentication, limits & quota (Block 3)

Create API keys with the CLI (the token is shown **once**):

```bash
inference-engine keys create --label my-app   # prints the token; save it
inference-engine keys list                     # ids/prefixes/labels — no secrets
inference-engine keys revoke <key-id>
```

Send the key as `Authorization: Bearer sk-ie-…` (or `x-api-key:`). Whether keys
are **required** depends on the bind:

- **Loopback (default):** auth is optional (dev convenience); any/no key is
  treated as `local`.
- **Network bind:** auth is required by default (see below).

Each authenticated request is subject to per-key limits (config, `0` = unlimited):
request body size (`max_request_bytes`), requests/minute (`rate_limit_per_min`),
concurrency (`max_concurrent_per_key`), and a **compute-unit (CU) quota** — a
5-hour rolling window (`quota_5h_cu`) and a weekly fixed cap (`quota_weekly_cu`),
where `CU = prompt_tokens·w_in + completion_tokens·w_out`. Responses carry
`X-RateLimit-*-CU-5h` / `-Week` headers; exceeding a limit returns `429` with
`Retry-After`. Over-limit is fail-closed.

### Network binding & TLS

The server binds to `127.0.0.1` by default. To bind to a LAN/public interface you
must **explicitly opt in** and address transport security yourself:

```bash
IE_ALLOW_NETWORK_BIND=true \
  inference-engine serve --host 0.0.0.0 --port 8000
```

Binding non-loopback **requires authentication** unless you also set
`IE_ALLOW_INSECURE_BIND=true` (not recommended). The engine does **not** terminate
TLS itself — run it behind a reverse proxy (nginx/Caddy/Traefik) or a private
network (e.g. Tailscale/VPC) that provides TLS. Refusing to start with a clear
error is intentional when these choices are not made explicitly.

### Health endpoints

| Endpoint    | Meaning                                                                 |
|-------------|-------------------------------------------------------------------------|
| `GET /healthz` | Process **liveness**. `200` with `{"status":"ok", ...}` when running. |
| `GET /readyz`  | **Readiness** of foundational dependencies (database reachable, migrations applied). Returns `200`/`503`. Reports `inference.available` (and the loaded `model_id`) separately — the service can be ready without a model loaded. |

Example:

```bash
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok","version":"0.0.0"}

curl -s http://127.0.0.1:8000/readyz
# {"status":"ready","version":"0.0.0","checks":{"database":"ok","migrations":"applied"},
#  "inference":{"available":true,"model_id":"my-model","state":"ready"}}
```

## Operability (Block 4)

The engine exposes enough evidence to understand health, load, and individual
request failures — without leaking prompts, responses, or secrets. Full metric
definitions and limitations are in [docs/operability.md](docs/operability.md).

**Live streams** (WebSocket) and their REST fallbacks:

| Surface | Purpose |
|---|---|
| `WS /ws/metrics` | Periodic resource + counter snapshots (an immediate snapshot is sent on connect). |
| `WS /ws/feed` | Inference live feed: request start / progress / end / error, with a bounded ring buffer replayed to late joiners. |
| `GET /metrics` | The current snapshot as JSON (fallback for the streams). |
| `GET /logs` | Recent structured log events (filter by `level`, `request_id`). |
| `GET /diagnostics` | A redacted support bundle (config + hardware + metrics + recent logs). |

All five are gated to **loopback development use or an authenticated operator**
(a valid API key via `Authorization: Bearer …`, `x-api-key:`, or `?api_key=` on
the WebSocket). Example:

```bash
curl -s http://127.0.0.1:8000/metrics | jq .energy
# measured only where a validated power probe (Linux RAPL) exists, else:
# {"state":"unavailable","watts":null,"j_per_token":null,"tokens_per_joule":null,...}
```

- **Request telemetry:** total/active/error request counts, prompt/completion
  tokens, requests-per-minute, per-request TTFT and total latency (on feed
  `request.end`), and backend state.
- **Process telemetry:** CPU %, memory, disk (of the data dir), process RSS, and
  uptime (via `psutil`). GPU is reported as an explicit **unavailable** state —
  no tested GPU probe ships yet.
- **Energy:** energy-per-token is **measured** only where a validated power probe
  (Intel/AMD **RAPL** on Linux) is readable; otherwise the state is
  **unavailable**. TDP-derived estimates are never presented as measured energy.
- **Error taxonomy:** every failure is classified as `validation` / `auth` /
  `limit` / `model` / `backend` / `cancellation` / `internal`, and errors carry an
  `x-request-id` correlation header. A failed request writes a structured
  `log_events` row (category + stage + stacktrace, correlated by request id).

## Model lifecycle (Block 6)

Models are managed through a **registry** (persisted in SQLite) rather than a
single hand-set `model_path`. The dashboard's **Models** page and the admin API
cover the whole lifecycle:

| Endpoint | Purpose |
|---|---|
| `GET /admin/models` | List registry models with progress + host-compat. |
| `POST /admin/models/import` | Register a local GGUF file (SHA-256 verified, metadata probed). |
| `POST /admin/models/download` | Start a verified download — Hugging Face (`repo` + `filename`) or a direct `url`, with optional `expected_sha256`. |
| `GET /admin/models/{id}` | One model — poll download progress. |
| `POST /admin/models/{id}/cancel` | Cancel an in-progress download (resumable `.part` kept). |
| `POST /admin/models/{id}/load` · `/unload` | Load a registry model (becomes the active model) / unload it. |
| `DELETE /admin/models/{id}` | Remove a model (unload it first). |

- **Verified & recoverable:** downloads stream to a `.part` file, resume via HTTP
  `Range`, and are checked against `expected_sha256` before being committed;
  imports hash the file on registration. GGUF **arch / quant / context length** are
  read from the file header without loading it.
- **Honest host compatibility:** each model reports `ok`, `too_large`, or
  `needs_backend` — the last when `llama-cpp-python` isn't installed or the CPU
  lacks AVX2. A downloaded model is **never** implied runnable just because it's
  listed. (Model *catalog search/discovery* and non-text modalities are later work.)
- A configured `model_path` is auto-registered as the active model on startup, so
  existing setups keep working.

Models live under `<data_dir>/models/` (configurable via `models_dir`).

## Controlled concurrency (Block 7)

An **admission scheduler** sits in front of the backend so the engine degrades
predictably under load instead of failing requests outright or exhausting memory.
The `llama.cpp` runtime serializes decoding per model context, so the honest
default concurrency is **1**; overlapping requests wait in a bounded queue and run
in order, and only excess load beyond the queue is shed:

- **Bounded admission.** `max_concurrency` slots gate concurrent generations;
  waiting requests queue up to `max_concurrency_queue`, for at most
  `concurrency_queue_timeout_s`. Past either bound, the request is shed with
  `429 engine_saturated` and a `retry-after` header (SDK clients retry it).
- **Capacity is always released** — on completion, failure, and **client
  disconnect** (a disconnected stream aborts its generation and frees the slot
  promptly).
- **Slow consumers can't grow memory.** Tokens flow through a bounded, semaphore-
  guarded queue, so a client that reads slower than the model generates applies
  backpressure to the producer instead of buffering the whole response.
- **Observable.** `GET /metrics` (and `WS /ws/metrics`) carry a `scheduler` panel
  (slots in use, queue depth, admissions, rejections, cancellations, slow-consumer
  events, and queue wait-time); the live feed emits `request.rejected` on shedding.

Configure with `max_concurrency`, `max_concurrency_queue`, and
`concurrency_queue_timeout_s`. Full policy, metric definitions, and the load-test
profile are in [docs/concurrency.md](docs/concurrency.md);
`scripts/load_test.py` drives a running engine and prints an admission/latency
summary.

## Operator dashboard (Block 5)

A small React/Vite single-page app — the local operator UI — is served by the
engine at **`/dashboard`** (the root path redirects there). Four views, all driven
by the typed admin API and the Block 4 WebSocket feeds:

- **Overview** — health, loaded model + state, uptime, request/token counters
  (including in-flight, queued, and shed requests from the admission scheduler),
  animated CPU/RAM/disk meters, energy state, and the live inference feed. The
  metrics stream reconnects automatically and falls back to REST polling of
  `/metrics`; the connection state is always shown.
- **Models** — the model **registry**: download from Hugging Face (repo + file) or
  a direct URL, or import a local GGUF; each add is **SHA-256 verified** with
  resumable, cancellable downloads and a live progress bar. Rows show probed GGUF
  metadata (arch · quant · size), a **host-compatibility** badge, and safe
  **load / unload / delete / set-active** actions.
- **Logs** — recent request/log events filtered by level and free-text (request
  id, model, error category), with per-row category and stage.
- **API keys** — create (with a label), copy the new secret **once**, and revoke.

**Access:** the SPA is served to anyone who can reach the route, but every data
endpoint it calls is gated (loopback dev use or a valid operator API key), so the
UI is useless without authorization when the engine is network-bound — access
control never relies on the obscurity of the route. When a call returns `401`, the
dashboard prompts for an operator key (sent as a Bearer token and, for WebSockets,
an `?api_key=` parameter).

The admin API (all operator-gated):

| Endpoint | Purpose |
|---|---|
| `GET /admin/overview` | Health, readiness, configured model, and current metrics. |
| `POST /admin/model/load` · `POST /admin/model/unload` | Safe, idempotent model control. |
| `GET /admin/keys` · `POST /admin/keys` · `DELETE /admin/keys/{id}` | Key management (token shown once). |

Build the dashboard into `engine/static` (bundled into the wheel by CI):

```bash
cd dashboard
npm ci
npm run test:run   # vitest component tests (loading/disconnected/error/populated)
npm run build      # outputs to ../engine/static, served at /dashboard
# dev with hot reload against a local engine on :8000:
npm run dev
```

A control-plane smoke test drives the whole operator flow (model load → generation
→ metric update → log inspection → key revoke) against a running engine:

```bash
python scripts/dashboard_smoke.py --base-url http://127.0.0.1:8000 [--api-key sk-ie-…]
```

## Deployment (Block 9)

A supported, reproducible deployment path via Docker, with graceful shutdown,
readiness gating, and backup/restore. Full details — volumes, reverse-proxy/TLS,
upgrades, the supported-platform matrix, and supply-chain basics — are in
[docs/deployment.md](docs/deployment.md).

```bash
docker compose up --build      # or: docker build -t inference-engine . && docker run ...
```

- **Graceful drain:** on `SIGTERM` the engine stops admitting work, lets in-flight
  generations finish (up to `drain_timeout_s`), then releases the model — so
  rolling restarts never sever active requests. `/readyz` reports not-ready while
  draining so a balancer stops routing.
- **Readiness gating:** set `require_model_ready=true` so `/readyz` is not-ready
  until a model is loaded. Liveness is `/healthz`; `/version` reports the build.
- **Backup & restore** (DB + config, checksum-verified; model files excepted):

  ```bash
  inference-engine backup  --out /backups
  inference-engine restore --from /backups/inference-engine-backup-*.tar.gz
  ```

- **Supply chain:** `/version` reports the build commit/date; every model records a
  SHA-256; generate an SBOM with `python scripts/generate_sbom.py --out sbom.json`.

## Security (Block 9b)

Hardening for exposed/production deployments, all defaulting to permissive-but-safe:
an **IP allowlist** (`ip_allowlist`, with optional `trust_forwarded_for` behind a
proxy), an **egress lock** and **feature kill switches** (`allow_network_downloads`,
`allow_model_management`, `allow_structured_output`, `diagnostics_enabled`), enforced
safe-format validation and log redaction (prompts/responses/secrets are never
logged), CI **`pip-audit`** + SBOM for CVE/inventory visibility, and a
**tamper-evident, hash-chained audit log** of operator/security actions
(`GET /admin/audit?verify=true`). Details in [docs/security.md](docs/security.md).

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
  also runs the dashboard control-plane smoke and the **model-lifecycle smoke**
  (import → load → generate → unload → delete on the real model), and runs on
  demand via **workflow_dispatch**.
- **`dashboard`**: installs the SPA deps, type-checks, runs the **vitest**
  component tests, and builds the dashboard into `engine/static` — the React/Vite
  build/test environment (Block 5).
- **`build`**: builds the operator dashboard, then the sdist + wheel with
  `python -m build` (so the wheel bundles the SPA), installs the wheel into a clean
  virtualenv, and smoke-tests the packaged CLI (`version` / `migrate`), the running
  server's health + `/metrics` + served `/dashboard`, and the dashboard
  control-plane smoke. The distributable is verified, not just the source tree.
- **`ci-success`**: a single aggregator gate that passes only when **all** of the
  above jobs succeed (`test`, `dashboard`, `integration-llama`, `build`; failing if
  any failed or was skipped). Use it as the one required status check.

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

## Project layout

```
engine/
├── main.py             # FastAPI app factory + lifespan (logging, migrations, sampler)
├── config.py           # layered pydantic-settings configuration
├── cli.py              # `inference-engine` CLI (serve/migrate/config/generate/keys)
├── gateway.py          # auth, limits, quota, attribution, operator gate
├── logging_setup.py    # structured JSON logging
├── api/
│   ├── health.py       # /healthz, /readyz
│   ├── openai_router.py# /v1/chat/completions, /v1/models (OpenAI dialect)
│   ├── anthropic_router.py # /v1/messages (Anthropic dialect)
│   ├── serving.py     # shared request lifecycle (auth, admission, cleanup)
│   ├── ws_router.py    # /ws/metrics, /ws/feed
│   ├── ops_router.py   # /metrics, /logs, /diagnostics
│   ├── admin_router.py # /admin/overview, /admin/model/*, /admin/keys
│   ├── deps.py         # operator access gate (loopback or valid key)
│   └── errors.py       # OpenAI error envelope + structured error logging
├── backup.py           # database + config backup/restore
├── buildinfo.py        # release/build metadata (version, commit, date)
├── inference/          # generation contract, llama.cpp backend, admission scheduler
├── models/             # registry, GGUF probe, downloader, host compat, lifecycle service
├── auth/ · quota/      # API keys · compute-unit quota
├── telemetry/          # event bus, counters, resources, power, logs, diagnostics
├── static/             # built operator dashboard SPA (build artifact, served at /dashboard)
└── store/
    ├── db.py           # SQLite (WAL) connection
    ├── migrations.py   # migration runner
    └── migrations/     # NNNN_*.sql migration files
dashboard/              # React/Vite operator UI source (builds into engine/static)
scripts/                # CI smokes (real-model SDK, dashboard control-plane)
tests/                  # pytest suite
```

## License

Proprietary. All rights reserved.
