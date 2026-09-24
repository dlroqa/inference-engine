# RFC: Block 12 — Dual-engine routing and pool scale-out

- **Status:** Draft (precondition artifact for Block 12; blocks slices 12.1–12.5)
- **Author:** Inference Engine
- **Created:** 2026-09-23
- **Supersedes / depends on:** Block 10 remote scale-out (complete). See
  `BLOCK_12_DUAL_ENGINE_ROUTING_INSTRUCTIONS.md`, `docs/backends.md`,
  `docs/compatibility.md`.

This RFC is the precondition the Block 12 instructions require before any code
lands: it records the user problem, target models, hardware, traffic shape,
privacy considerations, rollback plan, and success metrics, and it fixes the
**improvement threshold** and **regression budget** that gate every later
routing rule. Baseline numbers are produced by the slice 12.2 harness and pasted
back into this document before slice 12.3 enables anything.

---

## 1. Problem

Block 10 can run vLLM **or** SGLang behind the gateway, and can hold several
homogeneous workers in one pool (least-busy + prefix affinity + spillover). It
assumes **one `model_id` across the whole pool** (`registry.representative()`
reads a single id) and it makes **no adaptive routing decisions** — placement is
least-busy only, and `route_metrics` is measurement-only by design.

Operators now want vLLM and SGLang to run as **independent peer pools** behind
the same stable OpenAI/Anthropic gateway, each potentially serving a **different
set of models**, and they want the gateway to choose a pool using **auditable,
measured** signals rather than round-robin — without changing the public API
contracts, without engine chaining, and without becoming a per-token data-plane
balancer.

Concretely, three gaps:

1. **Heterogeneous eligibility.** A request for model *X* must only reach engines
   that actually serve *X* (and the feature subset it needs). Today the pool is
   homogeneous, so there is no eligibility filter.
2. **No baseline / benchmark.** There is no reproducible way to measure the
   current deterministic topology, so no routing change can be justified.
3. **No measured routing.** There is no mechanism to introduce a routing rule,
   prove it beats the deterministic baseline on target hardware, and roll it
   back.

## 2. Goals / non-goals

**Goals**
- vLLM and SGLang operate as independent, healthy **peers** behind the stable
  gateway; each declares which models it serves.
- Deterministic **model-to-engine eligibility** is enforced everywhere a
  placement is made; an incompatible engine is never selected.
- A reproducible **benchmark harness** produces the baseline metric set and a
  policy-vs-baseline comparison gated by a declared threshold and regression
  budget.
- Any workload-aware routing rule is **config-driven, observable, unit-tested,
  disabled by default, and reversible**, and is only enabled after a
  target-hardware benchmark clears the threshold.

**Non-goals (this block)**
- No new public API dialect; no undocumented compatibility claims.
- No engine chaining (SGLang behind vLLM or vice-versa), no cross-engine
  continuation, no transparent recovery after partial streaming.
- No per-token data-plane balancing in the gateway; per-engine pool routers
  (12.4) and advanced topology (12.5) are RFC-gated and deferred until a
  multi-worker GPU deployment justifies them.
- No prompt/completion content logging for routing analysis.

## 3. Target models

_To be finalized with the operator. Initial target set:_

| Model | Engine(s) that serve it | Feature subset | Notes |
|---|---|---|---|
| `<primary-chat-model>` | vLLM, SGLang | chat, streaming | overlap case — routable across both |
| `<sglang-only-model>` | SGLang | chat, streaming, prefix cache | eligibility must exclude vLLM |
| `<vllm-only-model>` | vLLM | chat, streaming, structured output | eligibility must exclude SGLang |
| `<local-gguf>` | llama.cpp (local) | chat, streaming | fallback; no KV/prefix-cache claims |

The **compatibility matrix by route** (§10) is the authoritative version of this
table and must stay accurate as models change.

## 4. Deployment hardware

_To be finalized._ Assumed initial topology (matches Block 10 exit state):

- Independently deployed remote GPU nodes, one per engine, reachable over
  authenticated TLS HTTP; the gateway runs separately (CPU host).
- No multi-GPU / Kubernetes / multi-region / prefill-decode disaggregation in
  this block (those are 12.5, separate RFCs).
- **Benchmarking constraint:** real vLLM/SGLang need GPUs the CI/sandbox hosts
  lack. As in Block 10, correctness runs against fake OpenAI-compatible servers
  in CI; **real baseline and policy numbers are produced by the operator running
  the 12.2 harness on target hardware.** Any rule whose justification requires
  those numbers stays disabled until they exist.

## 5. Traffic shape

The benchmark suite (12.2) must represent real traffic. Declared mix (tune to
the operator's measured distribution):

| Workload | Share | Why it matters for routing |
|---|---|---|
| Unique short prompts | 40% | baseline throughput / TTFT |
| Repeated-prefix prompts | 20% | prefix-cache reuse hypothesis (affinity) |
| Multi-turn sessions | 15% | session-affinity stability |
| Long context | 10% | engine long-context performance hypothesis |
| Structured output | 10% | feature eligibility (capability-gated) |
| Cancellation mid-stream | 3% | no re-route / no duplicate output |
| Saturation bursts | 2% | admission, shed, spillover behavior |

## 6. Privacy considerations

- Route decision logs/metrics record **engine, pool, route reason, fallback
  reason, upstream attempt count, queue wait, TTFT, output rate, total latency,
  outcome** — and **never** prompts, completions, credentials, or sensitive
  headers.
- Session affinity, if enabled, keys on a **privacy-safe** session identifier
  (opaque id / hashed key), never on prompt content.
- The benchmark harness uses synthetic or operator-supplied non-sensitive
  prompts; harness output contains metrics only.
- Existing egress allowlisting (Block 10.7) continues to bound any external
  spillover target.

## 7. Rollback plan

- Every routing rule ships **disabled by default** behind its own config flag
  with a tested disable path. Disabling all Block 12 rules returns placement to
  the **Block 10 deterministic model map** (least-busy within eligible engines).
- Model eligibility (12.1) is deterministic and always on; disabling *routing
  policy* never disables *eligibility* (an incompatible engine is never chosen
  regardless of flags).
- Config is reversible without a migration; no schema/data migration is
  introduced by routing (12.1–12.3). A bad rollout is corrected by flipping the
  flag off and reloading config.
- Post-first-token failures are reported honestly and never rerouted, so a
  rollback never produces duplicated or partial-then-retried output.

## 8. Success metrics

Measured by the 12.2 harness, per engine/pool and per virtual model:

- Request rate (sustained, accepted).
- **TTFT** (mean, p50, p95, p99).
- Token generation rate (output tokens/s).
- End-to-end latency percentiles (p50, p95, p99).
- Saturation / rejection (shed) rate.
- Failure rate (error finish / upstream failure), split pre-stream vs
  post-stream.
- Cost where measurable (per-1k-token weights, from `route_metrics`).

### 8.1 Improvement threshold (gate to enable a rule)

A workload-aware rule is enabled **only if**, on target hardware against the
baseline, it delivers a **≥ 15% improvement** in the metric it targets (e.g.
p95 TTFT for a prefix-affinity rule, or p95 latency for a long-context rule),
measured on the workload slice the rule claims to help. _Placeholder — confirm
per rule in §3 before enabling._

### 8.2 Regression budget (veto)

Enabling a rule must **not** exceed any of:

- Overall error rate: **+0.5 percentage points** vs baseline.
- p95 end-to-end latency on any other workload slice: **+5%**.
- Shed rate under the saturation burst: **+2 percentage points**.

A rule that clears §8.1 but violates §8.2 stays disabled.

## 9. Baseline data (Block 10 topology)

_Produced by the 12.2b harness (`python -m engine.bench run`, see
[../benchmarks.md](../benchmarks.md)); this table stays a **template** until an
operator records real target-hardware runs, then paste the safe command,
hardware/config identity, and measured values._

| Metric | Value | Harness command | Hardware / config id |
|---|---|---|---|
| Request rate (accepted/s) | _tbd_ | `<cmd>` | `<id>` |
| TTFT p50 / p95 / p99 (ms) | _tbd_ | | |
| Output rate (tok/s) | _tbd_ | | |
| Latency p50 / p95 / p99 (ms) | _tbd_ | | |
| Shed rate (%) | _tbd_ | | |
| Failure rate pre/post-stream (%) | _tbd_ | | |
| Cost /1k tokens | _tbd_ | | |

## 10. Compatibility matrix by route (skeleton)

Extends `docs/compatibility.md`; must state model **and** feature support per
route and stay accurate.

Concrete example matching `docs/backends.md` and `docs/compatibility.md` (replace
with the finalized target models from §3):

| Client model name | Policy | Eligible engines | Streaming | Structured output | Prefix cache | Notes |
|---|---|---|---|---|---|---|
| `chat-8b` | model-map | vLLM + SGLang | ✅ | per engine | per engine | homogeneous sub-pool (overlap) |
| `coder-7b` | model-map | SGLang | ✅ | per engine | ✅ | eligibility excludes vLLM |
| `<virtual-name>` | route/cascade | operator subset | ✅ | only if every selectable engine supports it | — | policy over the eligible set |

Delivered in slice 12.1: model eligibility, feature-safe placement, the
404/503/429·529/400 status contract, and this matrix are enforced at placement
(not merely documented). Adaptive/benchmark-gated routing remains 12.2–12.3.

## 11. Slice plan

| Slice | Scope | Benchmark-gated? | Status in this environment |
|---|---|---|---|
| 12.0 | This RFC + metric definitions | no | **this document** |
| 12.1 | Heterogeneous model eligibility (deterministic) | no | buildable + fully testable |
| 12.2a | Route-decision observability enrichment | no | done (merged) |
| 12.2b | Reproducible benchmark harness + §8.1/§8.2 comparison gate ([benchmarks.md](../benchmarks.md)) | harness runs on fakes in CI, real numbers on GPU | buildable + CI-testable |
| 12.3 | Measured workload-aware routing rules (disabled by default) | **enablement** gated on §8 benchmarks | scaffolding + unit tests buildable; enablement operator-gated |
| 12.4 | Per-engine pool data-plane router | yes (multi-worker GPU) | RFC + `DEFERRED` |
| 12.5 | Topology evolution (multi-GPU/k8s/multi-region/disaggregation) | yes | separate RFC(s) + `DEFERRED` |

## 12. Open questions

1. Final target model list and per-engine availability (§3).
2. Target hardware identity for the baseline (§4, §9).
3. Confirmed traffic distribution from operator telemetry (§5).
4. Per-rule improvement thresholds — is a single 15% default acceptable, or
   per-rule values (§8.1)?
5. Which 12.3 rule is introduced first (recommended: explicit
   capability/response-format eligibility, since it is auditable and not a
   performance hypothesis)?
