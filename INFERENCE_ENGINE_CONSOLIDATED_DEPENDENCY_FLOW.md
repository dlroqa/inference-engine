# Inference Engine — Consolidated Dependency Flow

Use this document together with:

1. `as-an-ai-engineer-binary-wave.md` — authoritative final architecture and feature inventory.
2. `VERTICAL_SLICE_BUILD_INSTRUCTIONS.md` — base vertical-slice build criteria.
3. `VERTICAL_SLICE_BUILD_INSTRUCTIONS_ALIGNED.md` — original-plan traceability and scope alignment.

The original architecture plan is not to be edited. This flow only establishes the order in which its capabilities are built.

## Governing rule

Every block and sub-slice must be deployable, testable, documented, and useful before its successor begins. If an exit criterion fails, fix the current block; do not bypass it by building later architecture.

## Complete dependency flow

```text
0  Foundation
   configuration • app factory • SQLite/WAL • migrations • structured logs • health • CI
   ↓
1  Local inference runtime
   GenerationRequest/token stream • one GGUF model • llama.cpp worker boundary
   ↓
2  OpenAI API contract
   chat completions • SSE • models • compatibility matrix
   ↓
3  Secure local gateway + quota foundation
   API keys • request limits • compute units • 5-hour rolling + weekly caps
   ↓
4  Operability core
   metrics • logs • event bus • WebSocket feeds • measured/unavailable energy state
   ↓
5  Minimal operator UI — MVP cutoff
   overview • model load/unload • logs • API key controls
   ↓
6  Model lifecycle
   registry • checksum/import/download • host compatibility • presets
   ↓
7  Controlled concurrency
   bounded queues • cancellation • admission control • load tests
   ↓
8  Anthropic + structured output
   Messages API • supported sampler controls • grammar/JSON constraints
   ↓
9  Production deployment + hardening baseline
   Docker • TLS/proxy guidance • backup/restore • drain • audit • safe defaults
   ↓
10a First remote worker
    one of vLLM/SGLang • health • model mapping • safe pre-stream failover
   ↓
10b Additional scale-out sub-slices
    second backend/Triton → routing/cache metrics → auto-models/cascades →
    gRPC/Kubernetes/advanced serving → external-provider spillover
   ↓
11a First commercial lifecycle
    Stripe webhook • idempotency • plan entitlement • usage reconciliation
   ↓
11b Commercial/client sub-slices
    outbound webhooks → additional providers → client account API → client SSE →
    Clients/Live Monitoring UI
   ↓
12  Advanced capabilities — one independently gated slice at a time
    RBAC/OIDC → orgs → embeddings/rerank/RAG → tools/MCP → response cache → LoRA →
    guardrails/incidents → Postgres/HA/installers
```

## Dependency rules

- Blocks 0–5 are the local, useful MVP path. Do not begin Blocks 6–12 merely because they are listed.
- Block 8 depends on the normalized API edge from Block 2 and the worker contract from Block 1. Never create a provider-specific inference runtime.
- Blocks 9–12 depend on authentication, limits, logging, eventing, and persistence completed in Blocks 0–4.
- Block 10 depends on the bounded concurrency and cancellation behavior from Block 7 plus the production trust boundaries from Block 9.
- Block 11 depends on durable compute/usage accounting from Block 3 and secure webhook/deployment controls from Block 9.
- Every Block 12 item requires its own short RFC, concrete user need, API contract, persistence model, security assessment, observability, tests, and rollback plan.

## Required stop gates

| Gate | Must be true before continuing |
|---|---|
| 0 → 1 | The app starts from documented config, migrations apply, and CI passes. |
| 1 → 2 | One local GGUF model streams through the internal contract without blocking the event loop. |
| 2 → 3 | The supported OpenAI compatibility matrix and SDK contract tests pass. |
| 3 → 4 | Keys, limits, attribution, and 5-hour/weekly quota behavior are tested. |
| 4 → 5 | Operators can diagnose request and backend failures from metrics/logs without secrets leaking. |
| 5 → 6 | The MVP is usable by an operator and has recorded real-user feedback. |
| 6 → 7 | Model import/download/load/unload is checksum-verified and recoverable. |
| 7 → 8 | Saturation, slow clients, cancellation, and queue limits are predictable under load. |
| 8 → 9 | Both documented API compatibility matrices and structured-output tests pass. |
| 9 → 10 | A supported deployment is secured, backed up, drained, restored, and release-tested. |
| 10 → 11 | Local and remote workers preserve the same supported client contract and health behavior. |
| 11 → 12 | Entitlements, webhook idempotency, quota enforcement, and usage reconciliation are auditable. |

## Constant structural constraints

- Keep one internal `GenerationRequest` and token-stream interface from Block 1 onward.
- Translate OpenAI and Anthropic only at the API edge; maintain a compatibility matrix rather than claiming universal parity.
- Keep inference workers conceptually isolated from the API process, even when early releases package both together.
- Do not add a feature without its API contract, persistence impact, security posture, observability, and tests.
- Defer smart routing, semantic security classifiers, RAG, MCP, and billing until the completed prior block proves the basic serving path is stable and useful.
