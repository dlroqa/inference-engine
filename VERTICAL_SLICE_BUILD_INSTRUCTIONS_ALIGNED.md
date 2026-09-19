# Inference Engine — Aligned Vertical-Slice Build Instructions

## Required reading order

Claude must read these two documents before starting implementation:

1. `/home/agent/.claude/plans/as-an-ai-engineer-binary-wave.md` — the authoritative final architecture, feature inventory, APIs, data model, and verification intent.
2. `VERTICAL_SLICE_BUILD_INSTRUCTIONS.md` — the base vertical-slice execution instructions.

This file is the **alignment overlay** that makes the base vertical-slice document conform to the original plan without attempting to build the whole system at once. It does not change or authorize edits to the original plan.

## Rule of precedence

The original plan owns the intended end-state. The vertical-slice documents own delivery order, scope gates, and acceptance criteria.

If they appear to conflict:

1. Preserve the original plan's end-state capability.
2. Deliver it at the later, appropriate vertical block rather than prematurely.
3. Document the narrowed initial scope and the remaining follow-on scope.
4. Do not silently delete, weaken, or claim completion of an original-plan feature.

## Core architecture that must remain unchanged

- Python 3.11+, FastAPI/Uvicorn, `llama-cpp-python`, and GGUF are the initial cross-platform serving foundation.
- The engine keeps one normalized `GenerationRequest` and async token-stream contract behind every public API.
- OpenAI and Anthropic dialects are API-edge translations; inference workers do not contain provider-specific request types.
- SQLite/WAL is the zero-configuration default. Alembic-style migrations, backup/restore, Postgres, and Redis remain planned for scale.
- SSE is the default inference streaming protocol. Dashboard WebSockets are for live operator state. gRPC is a later, separately secured service.
- The initial process may package API and worker together, but the worker boundary must be retained so local llama.cpp and later remote backends are interchangeable.
- Every capability needs an API contract, persistence impact, threat/security posture, observability, and tests proportionate to its risk.

## Final-scope traceability

| Original plan area | Required vertical delivery location |
|---|---|
| §1–§2 architecture, layout, configuration, SQLite | Blocks 0–1; extend in 6, 9, 12 |
| §3 llama.cpp, GGUF, async isolation, hardware probe | Blocks 1 and 7 |
| §3b scheduler/backpressure/cancellation | Block 7 |
| §3c–§3d energy, caching, efficiency | Block 4 interfaces; Blocks 7, 10, 12 measured enhancements |
| §4 OpenAI gateway and authentication | Blocks 2–3 |
| §4 Anthropic gateway | Block 8 |
| §4b SSE, WebSocket, gRPC | Blocks 2, 4–5, then Block 10 |
| §4c remote workers, routing, scaling | Block 10 |
| §5 telemetry, event bus, logs, dashboard | Blocks 4–6 |
| §5b sampling, presets, constraints | Block 8 |
| §5f catalog, downloads, compatibility, auto-models | Block 6, then Block 10 |
| §5h–§5j clients, key attribution, live monitoring, alerts | Blocks 3–4, then Block 11 |
| §5i hardening, audit, incidents, guardrails | Blocks 3, 9, 12 |
| §5k–§5m billing, webhooks, quotas, client realtime | Block 3 quota foundation; Block 11 |
| §5n MCP, retrieval, external providers, RBAC, LoRA, orgs, HA | Blocks 9–10 and 12 |
| §6 persistence, Postgres, migrations, backup | Blocks 0, 9, 12 |
| §7 packaging and cross-platform matrix | Blocks 1 and 9 |
| §8 layered configuration | Block 0; extend each later block |
| §10 verification | Apply relevant original tests as each block is delivered |

## Mandatory additions to the base vertical slices

### Block 0 — preserve configuration and persistence direction

Use `pydantic-settings`, `config.toml`, `IE_*` environment variables, and `platformdirs`. Begin with SQLite/WAL and migrations. Keep storage/config/log paths configurable and safe.

### Block 3 — build the quota foundation now

Along with keys and basic limits, implement the original plan's per-key compute-accounting core: estimated and actual compute units, 5-hour sliding window, weekly fixed cap, quota headers, and atomic updates. Do not build payment-provider integrations yet.

### Block 4 — distinguish measured from unavailable energy data

Keep the original energy-per-token direction, but only label J/token or tokens/J as measured where a validated platform probe is available. Use an explicit unavailable state otherwise. Do not display TDP-derived estimates as measured energy.

### Block 6 — preserve catalog and modality honesty

Start with supported GGUF text models, verified downloads, checksums, and host capability matching. The original Hugging Face catalog, embedding models, and discoverable image/audio/video/CV models remain later work. Until a suitable backend exists, show `Needs backend`; never imply a downloaded model is runnable merely because it appears in the catalog.

### Block 8 — preserve the full sampling direction

Add controls only if the chosen llama.cpp binding actually supports them, but retain the original planned progression: determinism/length, top-k/top-p/min-p/typical/TFS, Mirostat exclusivity, repetition controls, DRY, XTC, grammar/JSON-schema output, runtime/context tuning, speculative decoding, and persisted presets. Keep exact sampler ordering documented.

### Block 9 — include the hardening baseline

Before production deployment is declared complete, add trusted-network and IP policy, TLS/reverse-proxy guidance, egress policy, safe-format enforcement, kill switches for unsafe dynamic features, prompt/response log-redaction controls, patch/CVE visibility, and tamper-evident operator/security audit records. Advanced semantic detection and incident automation remain Block 12.

### Block 10 — required sub-slices after the first remote worker

Do not implement these in one step. Deliver and test them in this order:

1. First remote adapter: vLLM **or** SGLang, with secure credentials, health, explicit model mapping, timeout behavior, and safe pre-stream failover only.
2. Second remote adapter, then Triton only when justified by a real multi-model workload.
3. Backend capability registry, health-aware least-busy selection, and local/remote backend status.
4. Prompt/KV-cache metrics and prefix affinity only where the backend supports their semantics.
5. Named virtual auto-models, explicit route/cascade policies, dry-run explanation, fallback, and quality/cost measurement.
6. gRPC as an independently secured/deployed service, followed by Kubernetes topology, MoE, and prefill/decode disaggregation only when benchmarked.
7. External-provider spillover/failover with secrets, egress allowlisting, metering, and no silent failover after partial tokens are sent.

### Block 11 — required commercial sub-slices

1. Stripe-first lifecycle: verified inbound webhook, idempotency, plan entitlement, activate/suspend/revoke key, and metering reconciliation.
2. Standard Webhooks outbound signing, retries/backoff, dead-letter queue, delivery log/replay, and secret rotation.
3. Lemon Squeezy/Paddle only when their business need is confirmed.
4. Client-scoped account contract: `/client/me`, `/client/usage`, `/client/plan`, `/client/services`, published OpenAPI, and strict authorization isolation.
5. Client-scoped SSE: authenticated, `Last-Event-ID` recovery, reconciliation fetch, CORS restrictions, and polling fallback.
6. Full Clients/Live Monitoring UI: key-level attribution, error taxonomy, alerts, and cross-links to logs/security.

### Block 12 — retained advanced end-state, one capability per slice

The following are still in scope from the original plan. They are not optional omissions; select and deliver them individually as product need and operational readiness justify them:

1. Operator RBAC, OIDC/SSO, and operator audit.
2. Organizations/tenants and isolation.
3. Embeddings, reranking, vector collections, and RAG ingest/retrieve APIs.
4. Tool/function calling and MCP.
5. Exact response caching, then semantic caching only with strict isolation rules.
6. LoRA/adapter lifecycle.
7. Input/output moderation and guardrails.
8. Security detectors in Off/Monitor/Block modes, then incidents, evidence bundles, and safe containment playbooks.
9. Model integrity/verified checkpoints and recovery.
10. Postgres, backup/restore, warm-up/drain, autoscaling signals, and HA operating patterns.
11. Cross-platform installers only after each tested OS/accelerator release path is reliable.

## Security constraints retained from the original plan

- Use Monitor before Block for uncertain semantic detections.
- Default automatic response to reversible, high-confidence actions only; destructive remediation remains human-approved.
- Do not broadcast a client's IP address, credentials, incident details, or security notice to unrelated clients.
- Never log plaintext secrets. Prompts/responses are redacted by default outside explicitly controlled diagnostic modes.
- Model downloads, external providers, webhooks, and all egress paths require explicit allowlisting and secret handling.

## Practical MVP remains Block 5

The MVP is unchanged: a secure, observable, local GGUF engine with a documented OpenAI-compatible subset and a simple operator dashboard. It must have real operator use before advanced routing, semantic security, RAG, MCP, or billing is started.

## Per-block completion requirement

For every block, use the completion report in `VERTICAL_SLICE_BUILD_INSTRUCTIONS.md` and add:

```md
### Original-plan traceability
- Original-plan sections reviewed: <sections>
- Capabilities delivered now: <list>
- Capabilities deliberately deferred: <list and future block>
- Compatibility or behavior differences: <none, or exact documented difference>
```

No block is complete unless its base exit criteria, relevant original-plan verification cases, and this traceability section are complete.
