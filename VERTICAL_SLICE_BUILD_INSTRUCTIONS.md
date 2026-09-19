# Inference Engine — Vertical-Slice Build Instructions

## Purpose

Build the Inference Engine described in `as-an-ai-engineer-binary-wave.md`, but **do not implement it as one large program of work**. Implement the blocks below in order.

Each block is a vertical slice: it must be deployable, testable, documented, and useful before work begins on the next block. Preserve the architecture and naming direction in the source plan where it applies, but do not pull later-block functionality forward.

The target is a self-hosted, cross-platform inference engine based on Python, FastAPI, and `llama-cpp-python`, initially serving quantized GGUF models locally through an OpenAI-compatible API and a small operator dashboard.

## Non-negotiable operating rules

1. Work on **only one block at a time**. Do not start a later block until the current block meets every exit criterion.
2. Keep changes small, reviewable, and committed as coherent units. Do not rewrite completed blocks unless a defect or an explicit compatibility need requires it.
3. Before each block, inspect the existing repository, its tests, and prior implementation decisions. Do not assume files or APIs exist merely because the original broad plan named them.
4. Add automated tests with the implementation. A feature without a repeatable test is not complete.
5. Run applicable formatting, static checks, and tests before declaring a block complete. Report the exact commands and results.
6. Prefer safe, boring, documented libraries and platform behavior over speculative optimizations.
7. Never claim compatibility, performance, power efficiency, security detection, or cross-platform support that has not been tested and documented.

## Structural rules that keep the build healthy

- Keep one internal `GenerationRequest` and token-stream interface from Block 1 onward. API dialects only translate at the edge.
- Build a compatibility matrix rather than claiming universal OpenAI/Anthropic parity.
- Keep inference workers isolated from the API process boundary conceptually, even if early releases run both in one executable.
- Do not add a feature unless it has an API contract, persistence model, security posture, observability, and tests appropriate to its risk.
- Defer smart routing, security classifiers, RAG, MCP, and billing until the basic serving path has real usage data.

## Dependency flow

```text
Block 0  Foundation
    ↓
Block 1  Local inference runtime
    ↓
Block 2  OpenAI API contract
    ↓
Block 3  Secure local gateway
    ↓
Block 4  Operability core
    ↓
Block 5  Minimal operator UI  ← practical MVP cutoff
    ↓
Block 6  Model lifecycle
    ↓
Block 7  Controlled concurrency
    ↓
Block 8  Anthropic + structured output
    ↓
Block 9  Production deployment
    ↓
Block 10 Remote scale-out serving
    ↓
Block 11 Commercial controls
    ↓
Block 12 Advanced platform capabilities
```

## Architecture boundaries from the start

Use these boundaries even when the first implementation is a single executable:

```text
Client / Dashboard
       │
       ▼
FastAPI API edge
  - request validation
  - authentication and limits
  - OpenAI / Anthropic translation
       │
       ▼
Application services
  - GenerationRequest
  - model registry and lifecycle
  - telemetry and persistence
       │
       ▼
Inference worker boundary
  - backend interface
  - async token stream bridge
  - llama.cpp initially
```

The API edge must not call `llama-cpp-python` directly. It sends a normalized request to the inference-worker boundary. This allows later local workers, remote vLLM/SGLang workers, and controlled lifecycle management without changing public APIs.

---

# Block 0 — Foundation

## Objective

Create a reliable, minimally configured application skeleton that can start, validate its configuration, expose health endpoints, persist schema state, and run in CI.

## Build

- Create the Python 3.11+ project and package layout.
- Add a FastAPI app factory and a CLI command for starting the server.
- Add layered configuration with safe defaults: CLI flags > `IE_*` environment variables > config file > defaults.
- Add SQLite persistence with migrations from the outset. Keep the schema minimal at this stage.
- Add structured JSON logging with request-independent startup/error events.
- Add `/healthz` for process liveness and `/readyz` for readiness. Before a model exists, readiness may state that no inference model is loaded; it must be explicit and documented.
- Add formatting, linting/type checking where practical, and a test runner in CI.

## Do not build yet

- Model loading or inference.
- Dashboard, API-key auth, model downloads, remote binding, TLS, multi-user features, or GPU monitoring.

## Required tests

- App factory creates an application successfully.
- Invalid configuration fails early with a useful error.
- Configuration precedence is tested.
- Health endpoints return their documented responses.
- Migration applies to a new SQLite database.

## Exit criteria

- A new developer can clone, install, configure, start, and test the application using the README.
- CI runs the automated checks successfully.
- No endpoint implies that inference is available yet.

---

# Block 1 — Local inference runtime

## Objective

Serve one explicitly configured local GGUF model through a stable internal generation interface.

## Build

- Define the internal `GenerationRequest`, `GenerationChunk`, and final generation-result/error types.
- Define an `InferenceBackend` interface with only the capabilities currently needed: load, unload, health/capabilities, and generate an async token stream.
- Implement the initial local `llama-cpp-python` backend.
- Run blocking llama.cpp work outside the FastAPI event loop. Bridge generated tokens to the async application with a bounded queue.
- Support one loaded model at a time. Make load/unload state explicit: unloaded, loading, ready, generating, failed.
- Handle client cancellation and backend failures without leaving a worker stuck or falsely marked ready.
- Record only basic internal timings and token counts needed to validate behavior.

## Do not build yet

- Multiple resident models, LRU eviction, parallel slots, continuous batching, GPU auto-tuning, downloads, routing, or external APIs.

## Required tests

- A tiny, version-controlled or CI-provisioned GGUF fixture can load in an appropriate integration environment.
- Generation yields ordered token chunks and a terminal result.
- Loading failure leaves the backend in a recoverable state.
- Unload frees the runtime state.
- While generation is active, an async health-check test proves the event loop remains responsive.
- Cancellation ends generation and releases the worker.

## Exit criteria

- A local developer can generate a streamed response from one configured GGUF model.
- The internal API is independent of HTTP and provider dialects.
- The limitations of the initial backend are documented.

---

# Block 2 — OpenAI-compatible API contract

## Objective

Expose the supported local generation path through a narrow, documented OpenAI-compatible HTTP API.

## Build

- Implement `POST /v1/chat/completions` for a deliberately supported subset of chat-completions behavior.
- Implement streaming Server-Sent Events and non-streaming responses from the same normalized generation path.
- Implement `GET /v1/models` using the loaded/registered model identity.
- Add request validation, stable error payloads, request IDs, and cancellation propagation.
- Create a compatibility matrix in the README or dedicated document. State supported, unsupported, and partially supported OpenAI fields.
- Use `GenerationRequest` at the boundary: translate HTTP requests into it, and translate the internal stream back into OpenAI-shaped responses.

## Do not build yet

- Anthropic support, embeddings, function calling, images, batch APIs, Responses API parity, client accounts, or dashboard controls.

## Required tests

- Official OpenAI Python SDK can call the server through `base_url` for each supported non-streaming and streaming case.
- Unsupported parameters fail with a clear documented error; they must not silently behave differently.
- SSE output has valid OpenAI-compatible chunk framing for supported cases.
- A disconnected streaming client cancels the internal generation.

## Exit criteria

- The supported compatibility matrix is published and tested.
- An OpenAI SDK client can use the engine without custom protocol code for the supported surface.

---

# Block 3 — Secure local gateway

## Objective

Make the API safe to use on a trusted network and attributable to an API key.

## Build

- Add API-key creation, display-once issuance, hashing at rest, verification, revocation, and last-used timestamps.
- Require authentication for inference endpoints by default once the server is not strictly loopback-bound.
- Accept the documented bearer-key format initially.
- Add basic request-size, token, rate, and concurrent-request limits.
- Bind to loopback by default. Require an explicit configuration choice for LAN/public binding and emit a prominent warning for insecure configurations.
- Log request ID, endpoint, model, key identifier, status, timings, and token counts. Do not log raw credentials.
- Document TLS/reverse-proxy requirements; do not implement a public-internet deployment shortcut.

## Do not build yet

- Organizations, billing plans, OIDC/SSO, automatic threat detection, IP intelligence, client portals, or webhooks.

## Required tests

- Missing, invalid, and revoked keys are rejected.
- A valid key can access the supported API.
- Limits produce predictable responses and do not crash the inference worker.
- Logs contain attribution without exposing secrets.
- Unsafe network-binding configuration produces the documented warning/error.

## Exit criteria

- The engine has a defensible trusted-network default.
- Every inference request is attributable to a key and request ID.

---

# Block 4 — Operability core

## Objective

Give an operator enough evidence to understand engine health, load, and individual request failures.

## Build

- Add request lifecycle telemetry: request count, prompt/completion token counts, TTFT where measurable, generation rate, total latency, and backend state.
- Add process telemetry: CPU, RAM, disk use, process RSS, uptime; add GPU data only when a tested probe is available.
- Add a bounded in-memory event bus and a bounded recent-event buffer.
- Add `/ws/metrics` and `/ws/feed` only for authenticated operator use or loopback development use.
- Add structured error taxonomy: validation/auth/limit/model/backend/cancellation/internal.
- Add diagnostics export containing redacted configuration, hardware summary, and recent logs.

## Do not build yet

- Energy-per-token claims, SOC monitoring, semantic security detectors, alert-routing systems, high-cardinality analytics, or multi-node metrics.

## Required tests

- Metrics are emitted during a generation.
- WebSocket clients receive bounded, valid snapshots/events.
- A forced backend error yields a structured, correlated log record.
- No raw prompt, response, or secret is included in diagnostics by default.

## Exit criteria

- An operator can identify whether a failure originated in client input, auth/limits, model state, or backend execution.
- The metric definitions and limitations are documented.

---

# Block 5 — Minimal operator UI (MVP cutoff)

## Objective

Ship a small, useful dashboard for operating the local engine without turning the product into a full control plane.

## Build

- Add a React/Vite dashboard served by the engine.
- Include exactly these initial views:
  - Overview: health, loaded model, model state, uptime, request/token counters, CPU/RAM/disk, and tested GPU data.
  - Model control: configured model identity plus safe load/unload action.
  - Recent requests/logs: request ID, outcome, timings, model, key label, and error category.
  - Keys: create, label, revoke; show a new secret only once.
- Use typed admin API calls and the Block 4 WebSocket metrics/feed.
- Gate admin UI access appropriately; never rely solely on obscurity of the dashboard route.
- Keep the dashboard functional with WebSocket reconnect behavior and a REST snapshot fallback.

## Do not build yet

- Full model catalog, playground, advanced sampler panels, live SOC views, billing, integrations, or tenant management.

## Required tests

- Component tests cover loading, disconnected, error, and populated states.
- API tests enforce admin authorization.
- A manual smoke-test script verifies a model load, generation, metric update, log inspection, and key revocation.

## Exit criteria — MVP

The practical MVP ends here: a secure, observable, local GGUF engine with an OpenAI-compatible API and a simple dashboard. This is a coherent product, not merely a partial foundation.

Do not start Block 6 until this MVP has been used by real intended operators and the highest-friction gaps are recorded.

---

# Block 6 — Model lifecycle

## Objective

Make adding and managing supported text GGUF models reliable and safe.

## Build

- Add a model registry containing model identity, source, file path, checksum, size, quantization metadata, compatible backend, and lifecycle state.
- Support import from an approved local path and download from an allowlisted source.
- Implement resumable download where the chosen source supports it, checksum verification, disk-space preflight, and cancellation cleanup.
- Add safe delete with an explicit target and confirmation at the UI/CLI layer.
- Add a minimal per-model default configuration/preset mechanism.
- Extend the dashboard with installed-model listing and model detail.

## Do not build yet

- A universal Hugging Face catalog, arbitrary URLs, non-GGUF model formats, non-text modalities, LoRA, or automatic LRU model eviction.

## Required tests

- Valid and invalid checksum behavior.
- Interrupted download behavior.
- Registry persistence and migration behavior.
- Deletion only affects the selected registered model.
- Model metadata is visible through the API/UI without falsely claiming compatibility.

## Exit criteria

- A user can safely import or download a verified supported GGUF model, load it, use it, unload it, and remove it.

---

# Block 7 — Controlled concurrency

## Objective

Improve service behavior under load without overstating llama.cpp parallelism.

## Build

- Add bounded per-model admission queues and explicit queue limits.
- Define and document the initial concurrency policy for the actual llama.cpp runtime in use.
- Add queue wait-time, active-work count, rejection count, cancellation count, and slow-consumer metrics.
- Ensure client disconnects, timeouts, and failed writes release capacity.
- Return explicit saturation responses, including retry guidance where possible.
- Add load tests with a documented, repeatable hardware profile.

## Do not build yet

- Continuous batching guarantees, arbitrary `parallel` slots, prefill/decode disaggregation, remote load balancing, or performance claims unsupported by benchmarks.

## Required tests

- A slow consumer cannot grow the token buffer without bound.
- Queue saturation is bounded and produces expected responses.
- Cancellation frees capacity.
- Health and operator metrics remain responsive under load.

## Exit criteria

- The engine fails predictably and recoverably under supported load rather than silently degrading or exhausting memory.

---

# Block 8 — Anthropic compatibility and structured output

## Objective

Add only the provider features that the local backend can represent faithfully enough to document and test.

## Build

- Add `POST /v1/messages` and its supported Anthropic SSE event shape.
- Extend the compatibility matrix for both dialects.
- Add validated sampling controls that map to actual backend capabilities.
- Add JSON-object/JSON-schema or grammar-constrained output only after confirming the installed llama.cpp binding supports the exact mechanism.
- Keep provider-specific request/response translation at the API edge. Do not leak provider types into the inference worker.

## Do not build yet

- Universal tool-calling parity, multimodal message support, MCP, embeddings, or claims that every Anthropic/OpenAI SDK feature works unchanged.

## Required tests

- Official Anthropic SDK works for every documented supported flow.
- Stream event ordering and terminal events match the documented subset.
- Structured-output tests validate actual schema conformance for supported schemas.
- Unsupported features return clear errors.

## Exit criteria

- Both compatibility matrices are accurate, published, and protected by contract tests.

---

# Block 9 — Production deployment

## Objective

Make a supported server deployment reproducible, secure, observable, upgradeable, and recoverable.

## Build

- Ship a Docker deployment path first, with explicit volume, model-store, database, and reverse-proxy/TLS documentation.
- Add graceful shutdown/drain behavior: stop intake, allow bounded in-flight work to finish or time out, then release resources.
- Make readiness depend on the configured operational definition (for example, an optionally preloaded model is ready).
- Add backup/restore for SQLite database and configuration, with documented model-file backup expectations.
- Publish a supported-platform matrix. Test Linux CPU first; add macOS and Windows CPU paths when continuously tested.
- Add supply-chain basics: dependency lock policy, SBOM or equivalent inventory, model checksums, and release version metadata.

## Do not build yet

- Universal PyInstaller single binaries, Kubernetes orchestration, gRPC, multi-region HA, or GPU support claims not tested on real hardware.

## Required tests

- Container starts from a clean environment.
- Backup restores a working registry/configuration state.
- Graceful shutdown behaves as documented.
- Readiness/liveness behavior is testable.

## Exit criteria

- A documented supported deployment can be installed, backed up, upgraded, and recovered without manual database surgery.

---

# Block 10 — Remote scale-out serving

## Objective

Introduce a control-plane/worker split by connecting the stable gateway to remote compatible inference workers.

## Build

- Add a remote backend adapter for one chosen backend first: vLLM **or** SGLang, not both initially.
- Reuse `GenerationRequest` and internal token-stream semantics.
- Add backend registration, capability/health checks, timeouts, secure credentials, and explicit model-to-backend mapping.
- Add basic failover only for safe cases before streaming has begun. Do not promise transparent failover after partial tokens have reached a client.
- Move distributed rate/quota state to Redis only when multiple gateway replicas are actually deployed.
- Define the production topology and trust boundaries: gateway, worker, database, Redis, TLS termination, and secrets.

## Do not build yet

- Triton, automatic cascade routing, prefix-affinity algorithms, provider spillover, Kubernetes-specific optimization, or prefill/decode disaggregation.

## Required tests

- The same supported OpenAI API request can use local and registered remote backends.
- An unhealthy backend is not selected.
- Safe pre-stream failover behavior is tested.
- Secrets are never returned or logged.

## Exit criteria

- The gateway can route a documented supported request to a healthy remote worker without changing the client contract.

---

# Block 11 — Commercial controls

## Objective

Add access plans and billing integration only after the serving core is stable and there is a concrete commercial workflow.

## Build

- Add clients as owners of API keys, with basic plan/entitlement records.
- Add durable usage accounting and a clearly specified quota model.
- Integrate one billing provider first, preferably Stripe if usage-based billing is the requirement.
- Verify inbound webhooks before processing, store idempotency state, and process safely/retryably.
- Map plan changes to key access/rate/quota/model permissions.
- Add outbound usage events only after the accounting model is reconciled and auditable.

## Do not build yet

- Multiple payment providers, client-facing realtime portals, multi-tenant enterprise hierarchy, automatic collections workflows, or arbitrary outbound webhooks.

## Required tests

- Valid/invalid signature handling.
- Duplicate webhook delivery idempotency.
- Subscription activation, cancellation, and failed payment update entitlements correctly.
- Usage records reconcile with enforcement counters.

## Exit criteria

- A single documented paid-plan lifecycle reliably provisions, limits, and revokes API access.

---

# Block 12 — Advanced platform capabilities

## Objective

Add optional capabilities only when they solve validated user needs and can meet the same operational standard as prior blocks.

## Candidate work — select individually, never as a bundle

1. Operator RBAC and OIDC.
2. Organization/tenant isolation.
3. Embeddings and reranking.
4. Vector storage and RAG endpoints.
5. Tool/function calling and MCP.
6. LoRA/adapters.
7. Exact response caching; consider semantic caching only with strong isolation rules.
8. Additional remote providers or local backends.
9. Advanced routing/cascading.
10. Guardrails and security detectors in monitor-only mode first.
11. Energy measurement, only on platforms with validated measurement support.
12. gRPC, Kubernetes, and HA patterns.

## Rules for every selected capability

- Write a short RFC describing the user problem, supported contract, data classification, failure behavior, threat model, metrics, and rollback plan.
- Implement monitor/observe mode before automatically enforcing uncertain semantic decisions.
- Do not disclose other clients’ data, IP addresses, incident details, or security notifications to unrelated clients.
- Keep destructive or irreversible actions human-approved.
- Add compatibility, authorization, tenancy/isolation, and failure tests before release.

## Exit criteria

- The individual selected capability has an explicit user need, bounded scope, operational owner, tests, documentation, and rollback path.

---

# Completion reporting template

At the end of each block, report exactly:

```md
## Block <number> completion report

### Delivered
- <implemented behavior>

### Public/API contract added or changed
- <endpoint, schema, compatibility-matrix entry, or “none”>

### Persistence and migration impact
- <tables/migrations or “none”>

### Security and privacy posture
- <auth, validation, secret handling, data logging/redaction implications>

### Observability
- <logs, metrics, traces/events, dashboards>

### Verification performed
- `<command>` — <result>

### Known limitations
- <honest limitation and documented behavior>

### Exit-criteria status
- [ ] <criterion>
```

Only mark the block complete when every exit criterion is checked. If an exit criterion cannot be met, stop, explain the evidence, and propose the smallest correction before moving forward.

