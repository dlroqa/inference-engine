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

Backend *registry*, health-aware routing across several live backends, and
virtual auto-models are later Block 10 sub-slices; this page covers selecting a
single backend.

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

### Not yet (later sub-slices)

- Structured output (grammar / JSON-schema): the remote backends report
  `supports_structured_output = false`, so the edge rejects structured requests
  rather than returning unconstrained text, even though these servers can guide
  decoding.
- **Triton** — deferred by design until a real multi-model workload justifies it.
- A multi-backend registry with health-aware least-busy routing, prefix/KV-cache
  affinity, virtual auto-models, and external-provider spillover.

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
