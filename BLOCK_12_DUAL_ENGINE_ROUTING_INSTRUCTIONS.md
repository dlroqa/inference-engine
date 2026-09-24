# Block 12 Update — Dual-engine routing and pool scale-out

## Purpose

This is a follow-on instruction set for Block 12. It may begin only after Block 10's one-remote-engine gateway slice is complete, deployed, used by intended operators, and its exit criteria remain satisfied.

It adds vLLM and SGLang as independent peer pools behind the existing Inference Engine gateway. It does not change the public OpenAI or Anthropic API contracts.

## Preconditions

- Block 10 supports one selected remote engine through deterministic model-to-backend mapping.
- Gateway authentication, limits, quota accounting, telemetry, readiness, and drain behavior work for remote streams.
- Remote cancellation, timeouts, upstream health checks, and pre-first-token failover are tested.
- A short RFC records the user problem, target models, deployment hardware, traffic shape, privacy considerations, rollback plan, and success metrics.
- Baseline performance data exists for the Block 10 topology: request rate, TTFT, token generation rate, latency percentiles, saturation/rejection rate, failure rate, and cost where measurable.

## Architecture

```text
Clients
  │
  ▼
Inference Engine gateway
- OpenAI and Anthropic translation
- auth, limits, quota/accounting, admission, telemetry, drain
- deterministic model selection and measured routing policy
  │
  ├── SGLang pool
  │     └── optional SGLang-aware data-plane router when validated
  │
  └── vLLM pool
        └── optional vLLM-aware data-plane router when validated
```

The gateway performs semantic policy selection. It does not chain engines, reimplement KV-cache scheduling, or become a per-token data-plane balancer. vLLM and SGLang are peers.

## Build sequence

### Slice 12.1 — Second-engine registration

- Add the second remote engine using the established Block 10 remote-backend contract.
- Extend configuration with explicit model availability per engine and operator-selected default routes.
- Keep routing deterministic: model mapping or an explicit operator override only.
- Confirm local llama.cpp fallback behavior remains documented and unchanged.

### Slice 12.2 — Observability and benchmark harness

- Record selected engine/pool, route reason, fallback reason, upstream attempt count, queue wait, TTFT, output rate, total latency, and outcome without logging prompts, completions, credentials, or sensitive headers.
- Build a reproducible benchmark suite representing real traffic: unique prompts, repeated prefixes, multi-turn sessions, long context, structured output, streaming, cancellation, and saturation.
- Compare every proposed policy to the deterministic baseline. Define a minimum improvement threshold and an error/latency regression budget before enabling automatic routing.

### Slice 12.3 — Measured workload-aware routing

- Introduce one policy rule at a time, behind configuration and a disable/rollback switch.
- Begin with auditable edge signals only: explicit model capability, an operator-selected route, response-format requirement when capability is tested, and stable session affinity where privacy-safe.
- Treat claims about prefix reuse, long context, or structured-output performance as hypotheses. Enable a rule only after target-model and target-hardware benchmarks demonstrate the agreed improvement.
- Preserve deterministic model eligibility. Never route a request to an engine that does not support the requested model or documented feature subset.
- Fall back only before streaming begins. After the first client-visible token/event, report the documented partial-stream failure.

### Slice 12.4 — Per-engine pool routing, only when justified

- Add a dedicated data-plane router in front of a homogeneous engine pool only after that pool has multiple workers and measurements show it is needed.
- Evaluate SGLang Router, vLLM Router, llm-d, NVIDIA Dynamo, or another documented alternative through an RFC and test deployment; do not assume one is universally appropriate.
- The gateway routes to a pool endpoint; pool routers make engine-specific cache/KV/load decisions.
- Do not expose the pool routers publicly or duplicate gateway authentication, accounting, or provider translation in them.

### Slice 12.5 — Topology evolution, only when justified

- Start with independently deployed remote GPU nodes; document TLS, authentication, service discovery, secrets, health, and network boundaries.
- Add multi-GPU, Kubernetes, multi-region, or prefill/decode disaggregation only through separate RFCs with operational ownership and rollback procedures.
- Do not treat shared-GPU vLLM/SGLang VRAM partitioning as a supported production topology unless it is tested, documented, and capacity-bounded.

## Do not build

- Engine chaining: SGLang behind vLLM or vLLM behind SGLang.
- Unbounded retries, transparent recovery after partial streaming, or cross-engine continuation.
- Prompt/content logging for routing analysis by default.
- Heuristic routing without a deterministic fallback, observability, benchmarks, and a rollback switch.
- New public API dialects or undocumented feature compatibility claims.

## Required tests

- A supported OpenAI and Anthropic request reaches each eligible engine without a public API change.
- Model and feature eligibility prevent selection of an incompatible engine.
- Every routing rule has deterministic unit tests, a disabled/default state, and a tested rollback path.
- Route decision logs/metrics contain engine, pool, reason, latency, and result without sensitive request content or credentials.
- Session-affinity behavior, if enabled, is stable and privacy-safe.
- Pre-stream fallback is tested; post-stream failure never reroutes or duplicates output.
- Disabling workload-aware routing returns traffic to the Block 10 deterministic model map.
- Existing Block 10 local and remote backend tests remain green.
- A load test demonstrates that the enabled policy meets the declared improvement target without exceeding the agreed regression budget.

## Exit criteria

- Both engines operate as independent, healthy peers behind the stable gateway API.
- Routing is data/config-driven, observable, tested, reversible, and justified by target-environment benchmarks.
- Gateway security, accounting, readiness, and graceful drain work consistently across every selected engine and pool.
- Any pool router or advanced topology has an explicit RFC, documented trust boundaries, tested failure behavior, and a rollback procedure.
- Compatibility matrices accurately state model and feature support by route.

## Completion report

Use the completion reporting template in `VERTICAL_SLICE_BUILD_INSTRUCTIONS.md`. Include the benchmark commands, hardware/configuration identity, baseline and post-change metrics, enabled routing policy, rollback setting, and known limitations.
