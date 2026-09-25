# Block 12.3 — Measured workload-aware routing

## Purpose

Blocks 12.1 and 12.2 establish the non-negotiable substrate for this slice:
physical-model and feature eligibility are deterministic, route decisions are
secret-free and attributable, and the 12.2b harness can compare a candidate
against the deterministic baseline. This slice adds the *controlled mechanism*
for one workload-aware preference at a time. It does not authorize an unmeasured
policy to run in production.

The first candidate rule is **structured-output preference**: for one named,
physical model, prefer an operator-declared primary backend subset when the
request requires `structured_output`. It is deliberately narrow: it relies only
on an already parsed, capability-gated request feature and configured backend
names. It is not a claim that any engine is better at structured output. It
ships disabled; target-hardware evidence must clear the existing 12.2b gate
before an operator enables it outside an isolated benchmark deployment.

This is a preference inside the existing gateway placement layer, not a
per-token data-plane router. vLLM and SGLang remain independent peers.

## Required reading and alignment

Read these documents before implementation, in this order:

1. `BLOCK_12_DUAL_ENGINE_ROUTING_INSTRUCTIONS.md` — parent architecture and
   invariants for slices 12.1–12.5.
2. `docs/rfcs/block-12-dual-engine-routing.md`, especially §§5–9 and §11 — the
   traffic mix, privacy/rollback requirements, ≥15% improvement threshold, and
   regression vetoes.
3. `BLOCK_12_1_HETEROGENEOUS_MODEL_ELIGIBILITY_INSTRUCTIONS.md` — model and
   feature eligibility always apply before policy selection.
4. `BLOCK_12_2A_ROUTE_DECISION_OBSERVABILITY_INSTRUCTIONS.md` — decision field
   meanings and privacy contract.
5. `BLOCK_12_2B_BENCHMARK_HARNESS_INSTRUCTIONS.md` and `docs/benchmarks.md` —
   comparable-report and gate semantics.
6. `engine/inference/router.py`, `engine/inference/registry.py`,
   `engine/api/serving.py`, `engine/config.py`, and `docs/backends.md` — current
   placement, configuration, and operator contract.

The current RFC has no real GPU baseline in §9. Therefore this slice may add
scaffolding, documentation, and CI tests now, but no policy is enabled by
default and no completion report may claim target-hardware performance.

## Invariants

- **Eligibility first and always.** A request can only reach a ready backend
  whose live `capabilities().model_id` matches its physical model and whose live
  capabilities meet every required feature. A rule may narrow eligible choices;
  it never expands them.
- **Baseline remains the fallback.** With the global switch off, with an
  individual rule off, when a rule does not match, or when its preferred subset
  cannot admit work, placement is exactly the Block 12.1 deterministic model
  map: prefix affinity / least-busy within the normal eligible scope, then the
  existing spillover behavior.
- **Selection-time only.** A rule is evaluated before the registry lease and
  before response headers or a client-visible stream event. There is no retry,
  reroute, duplicate output, or cross-engine continuation after streaming begins.
- **No new public routing input.** OpenAI and Anthropic contracts, gRPC, model
  names, and status codes remain unchanged. The rule consumes only server-known
  route model, parsed required features, and configuration.
- **Privacy.** Do not derive a rule key from prompt/completion text, credentials,
  IP addresses, raw headers, or a hidden session identifier. Existing
  `prefix_affinity_chars` is not a new 12.3 signal; its performance claim is
  separately benchmark-gated and it remains disabled when set to `0`.
- **No policy changes to queueing or reliability.** Keep the scheduler,
  per-backend capacity, health probes, remote retry budget, spillover rules, and
  `404`/`503`/`429`/`529`/feature-error behavior intact.
- **One rule at a time.** Do not build a generic heuristic scorer, learned
  router, quality classifier, engine chaining, session-affinity API, or long
  context/prefix performance rule in this slice.

## Rule and configuration

Add explicit configuration types rather than overloading `VirtualModelSpec`:

```python
class WorkloadRoutingRuleSpec(BaseModel):
    name: str
    kind: Literal["structured_output_preference"]
    model: str  # one physical client-facing model id
    preferred_backends: list[str]  # non-empty, unique configured names
    enabled: bool = False


class WorkloadRoutingSettings(BaseModel):
    enabled: bool = False  # global immediate rollback switch
    rules: list[WorkloadRoutingRuleSpec] = Field(default_factory=list)
```

Use the repository's established flattened settings style if nested TOML would
be inconsistent, but retain the same semantics and defaults. A representative
configuration is:

```toml
# Block 12.3: both switches default false. Do not enable until the 12.2b gate
# has passed on the exact target model, hardware, and workload slice.
workload_routing_enabled = false

[[workload_routing_rules]]
name = "chat-8b-json-vllm"
kind = "structured_output_preference"
model = "chat-8b"
preferred_backends = ["vllm-a"]
enabled = false
```

Validate at startup:

- names are unique; model and backend lists are non-empty/non-blank and backend
  names are unique within a rule;
- `model` is a configured physical model, never a virtual-model name; and every
  preferred name is a configured primary-pool backend (not an external/spillover
  provider). A rule must not turn paid external egress into a preference;
- the configured model and preferred backends are not treated as proof of current
  liveness or feature support. Those checks stay live in the registry;
- a globally disabled setting accepts disabled rules without changing startup or
  routing. Invalid enabled-rule references still fail fast, avoiding a latent
  dangerous configuration;
- document `workload_routing_enabled = false` as the complete, immediate rollback
  path. No migration or persisted state is introduced.

Do not use report paths, prompt data, or credentials as rule configuration. The
12.2b gate is an operator approval artifact, described below; embedding mutable
benchmark files in the request hot path would make availability depend on local
filesystem state and would not solve the candidate-measurement bootstrap problem.

## Placement design

Create an immutable resolved rule type owned by `engine.inference.router` (for
example `WorkloadRoutingRule`) and pass the resolved ordered rules to `Router`
from `engine/main.py`. Keep the existing public `Router.acquire()` and `plan()`
call shapes compatible; add an optional internal policy argument only where it
does not leak into public APIs.

For `structured_output_preference`, match only when all are true:

1. global workload routing is enabled;
2. this individual rule is enabled;
3. the requested route is the rule's physical model (not a virtual route); and
4. `required_features` contains `"structured_output"`.

On a match, attempt registry acquisition over the rule's `preferred_backends`,
with the *same* physical `model` and `required_features` arguments used by the
baseline path. If that subset acquires a lease, attach the rule name to the
decision. If it is unloaded, full, or feature-ineligible, acquire over the
ordinary physical-model scope exactly once. The baseline acquisition then owns
the public error/status behavior. Never translate a preference miss into a
feature error, saturation, spillover, or retry that the baseline would not have
produced.

The rule must not override virtual route/cascade scopes. Those are already an
operator-selected route policy and may have intentionally bounded backend sets.
It must not change the registry's primary-before-spillover ordering. Preferred
backends are validation-limited to primary entries, and the fallback call retains
normal tier handling.

Apply this before the `generate()` call. Once the lease is acquired, all serving,
cancellation, terminal accounting, and pre-stream remote retry behavior remain
unchanged.

## Observability and route planning

Extend `RouteDecision` with a safe optional `workload_rule: str | None`; retain
the meaning of existing `reason`, `fallbacks`, `policy`, and `step` exactly.
`reason` still names the registry algorithm (`model_map`, `least_busy`,
`prefix_affinity`, or `affinity_fallback`), while `workload_rule` says which
configured preference actually acquired the lease. A rule that matched but fell
back to baseline has `workload_rule = null`; it was not the final routing rule.

Propagate this field through the single terminal-outcome helper in
`engine/api/serving.py`, the safe `engine.request` `route_decision` log, and
`RouteMetrics`. Add a sorted `workload_rules` count map to each
`/admin/routes` row. Keep metrics backwards compatible: existing keys and their
meaning remain unchanged, and an absent/null rule does not add a synthetic
counter.

Extend `POST /admin/route/plan` only with safe explanatory fields:

- `workload_rule`: matched rule name or `null`;
- `workload_rule_preferred_targets`: configured names only when it matches; and
- the resulting candidate/chosen view using the same model + feature filtering.

It must never expose prompts, raw headers, credentials, upstream URLs, report
files, or hardware identity. The plan endpoint remains a dry run and never
reserves capacity.

Document the new safe field in `docs/backends.md`, update
`config.example.toml`, and amend RFC §11 status only after implementation. Add a
short Block 12.3 subsection to `docs/benchmarks.md` that states exactly which
gate output and target identity the operator must retain for enablement.

## Benchmark and enablement procedure

The policy may be exercised in a dedicated, exclusive benchmark deployment to
produce a candidate report. It must remain disabled in ordinary deployments
until an operator records both reports and runs the existing pure gate:

```bash
# baseline: global/rule switch off
python -m engine.bench run --base-url https://gateway.example \
  --workload standard --model chat-8b --exclusive-target \
  --admin-api-key "$OP_KEY" --api-key "$GEN_KEY" --out baseline.json

# candidate: only this rule enabled, on the same target model/hardware/config
python -m engine.bench run --base-url https://gateway.example \
  --workload standard --model chat-8b --exclusive-target \
  --admin-api-key "$OP_KEY" --api-key "$GEN_KEY" --out candidate.json

python -m engine.bench compare --baseline baseline.json --candidate candidate.json \
  --target-metric ttft_p95 --target-slice structured_output --min-improvement 0.15
```

The command's zero exit is necessary but not sufficient documentation. Record
the safe commands, harness/version identity, target model, engine/pool config,
hardware identity, and gate result in RFC §9 / the completion report. Confirm
that all §8.2 vetoes pass: overall error rate no worse than +0.5 percentage
points, p95 total latency on every other slice no worse than +5%, and saturation
shed rate no worse than +2 percentage points. If reports are incomparable,
unavailable, or fail any check, keep the rule disabled.

No real target-hardware results currently exist in this repository. CI may prove
the disabled/default and enabled-with-fake behavior only; it may not claim the
15% result.

## Tests

All tests are deterministic and GPU-free. Add focused coverage in
`tests/inference/test_router.py`, `tests/inference/test_registry.py` where needed,
`tests/api/test_routes_api.py`, relevant serving lifecycle tests, and configuration
tests.

- Default settings and an individual disabled rule produce byte-for-byte-equivalent
  Block 12.1 placement decisions, including virtual route/cascade, prefix
  affinity, spillover, and errors.
- An enabled structured-output rule selects its preferred ready backend only when
  the physical model and required feature match; a text request, other model,
  virtual model, or global switch off follows baseline selection.
- A wrong-model, unhealthy, full, or structured-output-incapable preferred entry
  cannot be selected. When another eligible entry exists, baseline fallback
  selects it; when none exists, preserve the baseline `400`/`503`/saturation
  contract exactly.
- Preferred primary failure still permits existing spillover only when the normal
  baseline rules permit it. A configured external/spillover preferred backend is
  rejected at startup.
- Physical and feature eligibility are passed into *both* preference and baseline
  acquisition. A same-name but wrong live model is never used.
- A successful preference records `workload_rule`; a preference fallback records
  `null`; existing reason/fallback/policy/step values retain their prior values.
- Route-decision logs, route metrics, and `/admin/routes` show the safe rule
  identifier/count and exclude prompt, completion, API-key, authorization, and
  remote-URL marker strings.
- `/admin/route/plan` is truthful for matched and non-matched requests and has no
  side effects on in-flight counters.
- Existing OpenAI, Anthropic, gRPC, model-eligibility, remote retry, spillover,
  virtual-model, observability, and benchmark tests remain green.

## Verification

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m pytest -q
```

Then run the benchmark/gate procedure above on target GPU hardware before
enabling the rule for ordinary traffic. The configured GitHub Actions matrix
remains the release-quality source of truth.

## Explicitly deferred

- Enabling this rule without a passing target-hardware gate, or claiming an
  improvement from CI/fake-backend results.
- Prefix-reuse, long-context, cost, or quality-based preferences; each is a
  separate hypothesis, rule, benchmark target, and review.
- Stable session affinity: no privacy-safe public/internal session identifier
  exists today, and multi-turn benchmark history is not one.
- Multi-rule composition, scoring, learned/adaptive routing, and automatic
  policy tuning.
- Per-engine data-plane routers (12.4) and topology changes (12.5).

## Completion report

Use the template in `VERTICAL_SLICE_BUILD_INSTRUCTIONS.md`. Include the exact
rule configuration/default state, safe observability fields, test results, the
benchmark commands and hardware/config identity, gate output, rollback setting,
and known limitations. Leave the target-hardware enablement exit criterion
unchecked until the operator has supplied comparable passing reports.
