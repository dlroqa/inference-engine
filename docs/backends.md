# Inference backends

The engine serves generation through a single **backend** that implements one
internal interface (`InferenceBackend`). The API edge, scheduler, and
request-serving lifecycle never change when the backend does — they speak the
same `GenerationRequest` / token-stream contract regardless of what runs behind
it.

| `backend_kind` | Runtime | Where it runs | Extra |
| --- | --- | --- | --- |
| `llamacpp` (default) | llama.cpp via `llama-cpp-python` | in-process, local GGUF | `[llama]` |
| `remote_vllm` | vLLM OpenAI-compatible server | external HTTP endpoint | `[remote]` |
| `remote_sglang` | SGLang OpenAI-compatible server | external HTTP endpoint | `[remote]` |

Health-aware routing across several live backends is covered below; virtual
auto-models and routing *policies* are later Block 10 sub-slices.

## Remote backends: vLLM & SGLang (Block 10, sub-slices 1–2)

vLLM (`remote_vllm`) and SGLang (`remote_sglang`) both expose an
**OpenAI-compatible** server (`/v1/models`, `/v1/chat/completions`,
`/v1/completions`, SSE deltas, and a terminal usage chunk via `stream_options`),
so they share one adapter core (`OpenAICompatibleRemoteBackend`) and behave
identically — only the selected `backend_kind` differs. The engine keeps owning
auth, rate limits, quotas, admission control, telemetry, and the OpenAI **and**
Anthropic edges; the remote server only does the decoding.

Install the extra and point the engine at your server:

```toml
# config.toml
backend_kind    = "remote_vllm"                       # or "remote_sglang"
model_id        = "team-llama3"                       # what YOUR clients request
remote_base_url = "http://vllm.internal:8000/v1"      # or the server root
remote_model    = "meta-llama/Meta-Llama-3-8B-Instruct"  # what the server serves
# remote_api_key = "…"   # prefer IE_REMOTE_API_KEY (never commit secrets)
```

```bash
pip install "inference-engine[remote]"
IE_REMOTE_API_KEY="sk-…" inference-engine serve
```

Every setting is also available as an `IE_*` env var
(`IE_BACKEND_KIND`, `IE_REMOTE_BASE_URL`, `IE_REMOTE_MODEL`,
`IE_REMOTE_API_KEY`, …). `remote_base_url` accepts either the server root
(`http://host:8000`) or the OpenAI base (`.../v1`). SGLang typically serves on
port 30000; use its `--served-model-name` as `remote_model`.

### Behavior and guarantees

- **Explicit model mapping.** Clients request the engine's `model_id`; the
  backend sends the operator-configured `remote_model` upstream. Arbitrary
  client-supplied model names are never forwarded.
- **Secure credentials.** `remote_api_key` is sent as a Bearer token to
  `remote_base_url` only. It is never logged, echoed in `/healthz`, or included
  in error messages.
- **Health / readiness.** On load the backend calls `/v1/models` and refuses to
  become ready unless the remote actually serves `remote_model` (and adopts its
  `max_model_len` as the context length when advertised). With
  `require_model_ready = true`, `/readyz` stays 503 until the remote is verified.
- **Timeouts.** `remote_connect_timeout_s` bounds connection setup;
  `remote_read_timeout_s` bounds the gap between streamed tokens, so a stalled
  remote surfaces as an honest terminal error instead of hanging a request.
- **Safe pre-stream failover only.** A transient failure (connection error,
  timeout, or a 429/5xx) that occurs **before the first token** may be retried up
  to `remote_max_prestream_retries` times. Once any token has reached the client,
  an upstream error ends the stream honestly (finish reason `error`) — the engine
  never silently re-routes or replays after partial output. A 4xx is a hard
  failure and is never retried.
- **Concurrency.** A remote server manages its own parallelism, so the backend
  does not lock to one generation; engine-wide admission control (`max_concurrency`,
  Block 7) still gates overlap. Raise `max_concurrency` when the remote can serve
  several requests at once. See [concurrency.md](concurrency.md).

### Egress control

Outbound inference is a kill switch: set `allow_remote_backends = false`
(`IE_ALLOW_REMOTE_BACKENDS=false`) to refuse building any remote backend, e.g. in
an air-gapped deployment. The single `remote_base_url` is the egress allowlist
for these sub-slices; broader external-provider spillover with per-destination
allowlisting is a later sub-slice. See [security.md](security.md).

## Multiple backends & routing (Block 10, sub-slice 3)

The engine can run **several backends at once** — a local llama.cpp plus remote
vLLM/SGLang workers, or several remote workers — and route each request to the
**least-busy healthy** one. A single-backend setup is just a pool of one, so it
behaves exactly as before.

The primary backend is whatever `backend_kind` selects; add more with
`[[remote_workers]]` tables. By default every worker exposes the shared
`model_id` (a **homogeneous** pool, least-busy routed); to have engines serve
**different** models, give each an explicit `model_id` — see [Heterogeneous pools
& model eligibility](#heterogeneous-pools--model-eligibility-block-121) below.

```toml
model_path       = "/models/llama3.gguf"   # primary = local llama.cpp
primary_max_in_flight = 1                    # llama serializes; raise for a remote primary

[[remote_workers]]
name    = "vllm-a"
kind    = "remote_vllm"
base_url = "http://gpu-a:8000/v1"
model    = "meta-llama/Meta-Llama-3-8B-Instruct"
max_in_flight = 8

[[remote_workers]]
name    = "sglang-b"
kind    = "remote_sglang"
base_url = "http://gpu-b:30000/v1"
model    = "meta-llama/Meta-Llama-3-8B-Instruct"
```

**Selection.** Each admitted request goes to the ready backend with the fewest
in-flight generations (stable tie-break by declaration order), skipping any that
is unloaded, failed a health probe, or is at its `max_in_flight` capacity. If all
healthy backends are full the request gets the same retriable saturation as the
scheduler (429/529); if none is loaded it is a 503. The **global** Block-7
`max_concurrency` still caps total in-flight, so raise it to actually overlap
across backends (see [concurrency.md](concurrency.md)).

**Health.** `backend_health_interval_s` (default 10s; 0 disables) governs a
background liveness probe of remote workers (`/v1/models`); a worker that goes
down is skipped by routing until it recovers. Local backends use their state.

**Status.** `GET /admin/backends` (operator-gated) returns each backend's name,
kind, local/remote, state, availability, and in-flight count — secret-free, no
base URLs with credentials.

### Heterogeneous pools & model eligibility (Block 12.1)

By default a worker's client-facing name is the shared `"local-model"`, so the
pool is homogeneous. Set a distinct **`model_id`** per worker to run vLLM and
SGLang as **independent peer pools that serve different models**. `model` remains
the upstream/provider model name sent to the server; `model_id` is what clients
request:

```toml
model_id = "chat-8b"          # the primary (local) serves this client-facing id

[[remote_workers]]
name     = "vllm-a"
kind     = "remote_vllm"
base_url = "http://gpu-a:8000/v1"
model    = "meta-llama/Meta-Llama-3-8B-Instruct"   # upstream name
model_id = "chat-8b"          # same id as primary -> a homogeneous sub-pool

[[remote_workers]]
name     = "sglang-b"
kind     = "remote_sglang"
base_url = "http://gpu-b:30000/v1"
model    = "Qwen/Qwen2.5-Coder-7B"
model_id = "coder-7b"         # a *different* model -> its own eligible engine
```

**Eligibility is deterministic.** A request for model `X` is placed only on a
ready backend whose live `capabilities().model_id` is `X`; an engine that serves a
different model is never selected. `GET /v1/models` (and gRPC `ListModels`) return
the **union** of served ids plus any virtual-model names. Placement then applies
least-busy / prefix affinity *within* the eligible engines, exactly as before.

**Feature eligibility.** A request that needs a feature (currently structured
output, `response_format`) is placed only on an eligible engine whose live
capabilities support it — even if a same-model engine without it is less busy.
This check is bound to placement (it reserves the lease), so it cannot be raced.

**Status codes.** An unknown model is `404` (even when every backend is down). A
**configured** model with no ready eligible backend is `503` — its id stays
advertised so clients can discover it. An eligible model whose ready engines are
all at capacity gets the retriable saturation (`429` OpenAI / `529` Anthropic). A
model that has a ready engine but none support a required feature is `400
structured_output_unsupported`.

**Names are unique.** Duplicate backend names, and a virtual-model name that
collides with a physical model id, are rejected at startup. Loading a local model
whose id would collide with a configured virtual name is refused (`409`).

**Local fallback is unchanged.** The local llama.cpp backend advertises no prefix
or KV-cache support and, when configured, keeps serving its `model_id`; disabling
Block 12 routing leaves the Block 10 deterministic model map in place.

`GET /admin/backends` shows each backend's live `model_id` and its configured
`served_model` (so operators see why a model stays known while unavailable), and
`POST /admin/route/plan` accepts `{"model": ..., "required_features": [...]}` to
dry-run which engines are eligible.

### Prompt/KV-cache metrics & prefix affinity (sub-slice 4)

Where a backend actually maintains a prompt/prefix cache — vLLM and SGLang do, the
local llama.cpp path does not — the engine can route prefix-sharing requests to
the same worker and surface that worker's cache stats. Everything here is
**capability-gated and honest**: llama.cpp reports neither, and a remote whose
build has the feature off is configured to report it off.

### Measured workload-aware routing (Block 12.3)

CPU/RAM-only deployments continue using the deterministic model map by default.
`workload_routing_enabled = false` is the global rollback switch; individual
`workload_routing_rules` are also disabled by default. The initial
`structured_output_preference` rule may prefer a configured primary backend for a
physical-model request requiring structured output, but live model, feature, health,
and capacity eligibility still apply. Enabled rules are validated at startup: their
model must be a configured physical model and every preferred target must be in the
primary pool; virtual names and external/spillover providers are rejected. This
remains true even while the global switch is off, preventing latent unsafe
configuration. If the preference cannot serve the request, normal deterministic
placement resumes.

`POST /admin/route/plan` exposes `workload_rule` and
`workload_rule_preferred_targets` only when a rule matches. Its ordered steps show
the preferred attempt followed by the ordinary eligible scope when a fallback would
be needed; it is a dry run and never reserves capacity.

**Prefix affinity.** Set `prefix_affinity_chars = N` (default `0` = off). Each
request whose prompt shares the leading `N` characters is routed, via a consistent
hash, to the same prefix-cache-capable backend so its cache is reused. If that
backend is at capacity the request falls back to least-busy placement — affinity
never overloads one worker. Backends that don't support a prefix cache (or a pool
with none) always use plain least-busy routing (sub-slice 3). Disable a remote's
participation with `prefix_cache = false` on its config/worker entry.

**KV/prefix-cache metrics.** When `kv_metrics` is on (default), the background
health refresh also scrapes the remote's Prometheus `/metrics` for KV-cache
utilization and prefix-cache hit rate (vLLM `vllm:gpu_cache_usage_perc` /
`vllm:prefix_cache_*`, SGLang `sglang:token_usage` / `sglang:cache_hit_rate`).
`GET /admin/backends` then includes a `cache` block per capable backend and omits
it for the rest:

```json
{
  "name": "vllm-b", "location": "remote", "state": "ready",
  "supports_prefix_cache": true, "supports_kv_cache_metrics": true,
  "cache": {"kv_cache_utilization": 0.42, "prefix_cache_hit_rate": 0.75}
}
```

SGLang exports metrics only with `--enable-metrics`; without it, `kv_metrics` finds
nothing and the `cache` block is simply absent (no fabricated numbers). Live
verification needs a real vLLM/SGLang on a GPU host —
`python scripts/remote_smoke.py --backend vllm --base-url … --model … --show-cache`.

### Virtual auto-models & routing policies (sub-slice 5a)

A **virtual model** is a client-facing model name that maps to a routing policy
over the pool, rather than one physical model. Clients keep requesting a model by
name (OpenAI `model` / Anthropic `model`); the base served id always works, and
virtual names add named policies on top. Declare them as `[[virtual_models]]`:

```toml
# Route: serve from any backend in the set (least-busy / affinity within it).
[[virtual_models]]
name     = "fast"
policy   = "route"
backends = ["vllm-a", "vllm-b"]

# Cascade: try each step in order; the first step with an available backend
# serves the request (admission-time fallback across steps).
[[virtual_models]]
name  = "tiered"
policy = "cascade"
steps = [["vllm-a"], ["vllm-b", "sglang-a"]]
```

Backend names are `"primary"` plus each `remote_workers` name; an unknown name
fails fast at startup. Requesting a model that is neither the base id nor a
declared virtual name is a 404. `/v1/models` lists the base id and every virtual
name.

**Fallback is selection-time only.** A cascade escalates to the next step when a
step's backends are unavailable or at capacity; there is **no switch once tokens
have been sent** — the same rule as remote pre-stream failover. Within a step,
least-busy and prefix affinity (sub-slices 3-4) still apply.

**Dry-run.** `POST /admin/route/plan` (operator-gated) explains how a name would
route *right now* — the chosen backend, the candidates considered per step, and
their state/load — without reserving a slot or generating:

```bash
curl -X POST .../admin/route/plan -H "authorization: Bearer $KEY" \
  -d '{"model": "tiered", "prompt": "optional, for affinity"}'
```

#### Per-route cost & performance (sub-slice 5b)

Every served request is attributed to its `(virtual-model, backend)` pair, and
`GET /admin/routes` (operator-gated) reports the running totals — requests, errors,
cancellations, tokens, **token cost**, `success_rate`, and average total /
first-token latency — plus per-model **sheds** (admission failures) and overall
totals:

```json
{
  "routes": [
    {"model": "fast", "backend": "vllm-a", "requests": 120, "errors": 1,
     "prompt_tokens": 30000, "completion_tokens": 8000, "cost": 0.114,
     "success_rate": 0.9917, "avg_total_ms": 240.0, "avg_ttft_ms": 38.0,
     "avg_queue_wait_ms": 5.2, "avg_output_tps": 34.1, "upstream_attempts": 121,
     "tier": "primary", "reasons": {"model_map": 118, "least_busy": 2},
     "fallbacks": {}, "policies": {"base": 120}}
  ],
  "sheds": {"fast": 3},
  "totals": {"requests": 120, "errors": 1, "cost": 0.114, "sheds": 3}
}
```

**Cost** uses per-backend token weights: `cost = prompt/1000·cost_per_1k_input +
completion/1000·cost_per_1k_output` (0 = free, e.g. a local backend). Set
`primary_cost_per_1k_input/output` for the primary and `cost_per_1k_input/output`
per `remote_workers` entry.

This is **measurement only** — it does not change routing decisions (adaptive
routing is a later refinement) — and it does not attempt a "quality" score, which
needs evaluation harnesses (Block 12). Latency is recorded only for requests that
actually produced tokens (errors/cancellations are counted but excluded from the
averages). External-provider spillover is sub-slice 7.

#### Route-decision observability (Block 12.2a)

Every routed request also records *why* it landed where it did — the substrate the
Block 12.2b benchmarks and 12.3 policy evaluation report on. `/admin/routes` rows
gain these fields, and one secret-free `route_decision` JSON log (logger
`engine.request`) is emitted per terminal outcome:

| Field | Meaning |
| --- | --- |
| `backend` | configured backend name — the current **pool identifier** for operators |
| `engine` | backend kind (`llamacpp` local, `remote_vllm`, `remote_sglang`, external) |
| `tier` | `primary` or `spillover` (not an engine/pool id) |
| `reason` | the algorithm that chose the final entry: `model_map` (one ready eligible candidate for a physical model), `least_busy` (fewest in-flight among several), `prefix_affinity` (prefix-cache target), `affinity_fallback` (affinity target full → least-busy) |
| `fallbacks` | extra facts, deterministic order: `cascade_escalation` (a virtual cascade selected a step after step 0), then `spillover` (the entry is in the spillover tier) |
| `policy` / `step` | router policy (`base`/`route`/`cascade`) and the cascade step index |
| `queue_wait_ms` | scheduler admission wait (recorded for every routed request) |
| `ttft_ms`, `total_ms` | time-to-first-token and end-to-end latency (token-producing requests) |
| `output_tps` | **end-to-end** output rate `completion_tokens / (total_ms/1000)`, incl. TTFT; sampled only for successful, token-producing requests |
| `upstream_attempts` | remote generation POSTs made, **including the first** (1 = first-try success; 2 = one pre-stream retry); 0 for a local backend |
| `outcome` | `ok`, `error`, or `cancelled` (an explicit error wins over cancellation) |

`/admin/routes` additionally aggregates per-route `avg_queue_wait_ms`,
`avg_output_tps`, total `upstream_attempts`, the stable `tier`, and count maps of
the `reasons`/`fallbacks`/`policies` seen.

This is **measurement only** and changes no routing outcome, eligibility,
affinity, spillover, admission, retry, or public contract. The `route_decision`
log and `/admin/routes` **never** contain prompts, completions, credentials,
authorization or other sensitive headers, or remote URLs.

The reproducible benchmark harness that consumes these signals and gates a
candidate policy against a baseline (RFC §8.1/§8.2) is documented in
[benchmarks.md](benchmarks.md) (`python -m engine.bench`).

### External-provider spillover (sub-slice 7)

The pool can include **external providers** — OpenAI-compatible endpoints
(OpenAI, Together, Fireworks, OpenRouter, a hosted vLLM…) used **only as
overflow** when the local pool cannot admit a request. External egress is treated
as sensitive:

- **Off by default.** `allow_external_providers = false` unless you opt in.
- **Egress allowlisted.** Every provider's `base_url` host must match an
  `egress_allowlist` entry (exact host or `.suffix` domain); an empty allowlist
  with providers configured, or an off-allowlist host, **fails at startup**.
- **Secrets** go in each provider's `api_key` (prefer a secure source); they are
  sent only to that provider and never logged or echoed.
- **Metered** like any backend — their cost (usually non-zero) and latency show up
  per route in `GET /admin/routes`, and `GET /admin/backends` marks them
  `"tier": "spillover"`, `"external": true`.

```toml
allow_external_providers = true
egress_allowlist = ["openai.com", "together.ai"]

[[external_providers]]
name  = "openai"
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
# api_key via IE_ config / secret source, never committed
cost_per_1k_input  = 0.15
cost_per_1k_output = 0.60
```

**Spillover is preferred-last:** the registry always chooses a ready, non-full
*local* backend first and only falls to a spillover backend when none can admit.
The choice is made **once at admission** — consistent with the engine's rule,
there is **no re-route once tokens have been sent** to the client.

**Not (yet) failover on a generation error.** Because `generate()` streams lazily
(the upstream call and first token happen after response headers are sent),
re-routing on a mid-flight error would risk sending duplicated/partial output —
so cross-backend failover on generation failure is deliberately out of scope;
only admission-time spillover (saturation / unavailability) and the same-endpoint
pre-stream retry (sub-slice 1) apply.

### Not yet (later sub-slices)

- Structured output (grammar / JSON-schema): the remote backends report
  `supports_structured_output = false`, so the edge rejects structured requests
  rather than returning unconstrained text, even though these servers can guide
  decoding.
- **Triton** — deferred by design until a real multi-model workload justifies it.
- Prefix/KV-cache affinity, routing/cascade policies, virtual auto-models, and
  external-provider spillover.

### Verifying against a real server

vLLM and SGLang require a GPU, which hosted CI runners lack, so CI exercises the
shared remote adapter against a real OpenAI-compatible **model** server
(llama.cpp's OpenAI server on the AVX2 runner) using `scripts/remote_smoke.py`,
smoking both `--backend vllm` and `--backend sglang`. Operators run the same
script against their real deployment for a true end-to-end check:

```bash
python scripts/remote_smoke.py --backend sglang \
  --base-url http://sglang.internal:30000/v1 \
  --model meta-llama/Meta-Llama-3-8B-Instruct \
  --api-key "$REMOTE_API_KEY" --wait
```
