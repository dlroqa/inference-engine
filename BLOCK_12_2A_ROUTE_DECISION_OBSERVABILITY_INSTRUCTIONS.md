# Block 12.2a — Route-decision observability enrichment

## Purpose

Block 12.1 made physical-model and feature placement deterministic, but the
current route telemetry says only which backend served a completed request. This
slice adds the measurement needed by Block 12.2b benchmarks and Block 12.3
policy evaluation:

- selected backend, engine kind, and tier;
- selection reason and all applicable fallback reasons;
- router policy and cascade step;
- scheduler queue wait, upstream attempt count, TTFT, output rate, total latency,
  and terminal outcome.

It is strictly observability work. It must not change candidate eligibility,
prefix-affinity behavior, spillover behavior, admission, retry behavior, public
OpenAI/Anthropic/gRPC contracts, or routing outcomes.

The implementation must never log or expose prompt/completion content,
credentials, authorization headers, or other sensitive request headers.

## Existing behavior and definitions

- `SchedulerLease.waited_s` is the scheduler-admission wait and is the source for
  `queue_wait_ms` (`waited_s * 1000`).
- A remote upstream attempt is every HTTP generation `POST`, including the first
  call. A successful first call therefore records `1`; one pre-stream retry
  records `2`. It is not a retry count.
- `backend` is the configured `BackendEntry.name`; `engine` is
  `BackendEntry.kind`. Until a separate pool abstraction exists, the backend name
  is the pool identifier reported to operators.
- `tier` is `primary` or `spillover`; it is not an engine or pool identifier.
- `output_tps` is `completion_tokens / (total_ms / 1000)`. Record a sample only
  for successful, non-cancelled requests with `completion_tokens > 0` and
  `total_ms > 0`. This is an end-to-end output rate, including TTFT; document that
  meaning so future benchmarks use it consistently.
- Outcomes are exactly `ok`, `error`, and `cancelled`. An explicit generation
  error wins over cancellation.

## Decision model

Add this immutable value in `engine/inference/registry.py`:

```python
@dataclass(frozen=True, slots=True)
class RouteDecision:
    backend: str
    engine: str
    tier: Literal["primary", "spillover"]
    reason: Literal["model_map", "least_busy", "prefix_affinity", "affinity_fallback"]
    fallbacks: tuple[Literal["cascade_escalation", "spillover"], ...] = ()
    policy: Literal["base", "route", "cascade"] = "base"
    step: int | None = None
```

`reason` identifies the algorithm that selected the final entry:

- `model_map`: a physical-model request had exactly one ready, eligible,
  capacity-available candidate in its selected tier;
- `least_busy`: normal least-busy selection among multiple available candidates;
- `prefix_affinity`: a prefix-cache affinity target was selected;
- `affinity_fallback`: the affinity target was ready but full and least-busy
  selected another candidate.

`fallbacks` records additional routing facts without overwriting the primary
selection reason. Include `cascade_escalation` when a cascade selects a step
after step 0, and `spillover` when the selected entry is in the spillover tier.
The tuple is deterministic: cascade escalation first, spillover second when both
apply. A simple `fallback: str | None` is insufficient because both facts can be
true for the same request.

`RouteDecision` remains frozen. Build the full decision in the registry, or use
`dataclasses.replace` to return a new decision when router policy context is
resolved; never mutate a frozen decision attached to a lease.

`RegistryLease` gains `decision: RouteDecision | None = None`. A non-`None`
decision is required for every successfully acquired registry lease.

## Placement instrumentation

### `engine/inference/registry.py`

1. Refactor `_pick(pool, prefix_key)` to return `(entry, reason)`. It must retain
   the exact existing selection behavior:
   - ready, capacity-available prefix target -> `prefix_affinity`;
   - ready but full prefix target, then another least-busy candidate ->
     `affinity_fallback`;
   - ordinary candidate selection -> `least_busy`.
2. Add `_select_detailed(...) -> (entry, reason, tier, available_in_tier)`.
   Apply the existing allowed/model/feature filtering before tiering, affinity,
   and least-busy selection. Try primary then spillover exactly as today.
3. `available_in_tier` is the number of ready, eligible, capacity-available
   candidates in the selected tier before selection. Convert ordinary
   `least_busy` to `model_map` only when this count is exactly one and the
   request is a physical-model request. Do not label a multi-candidate
   least-busy selection as `model_map`.
4. Keep public `select()` signature and return type unchanged by delegating to
   `_select_detailed(...)[0]`.
5. `acquire()` uses the detailed result, increments capacity exactly once, and
   attaches a base `RouteDecision` with the selected entry name, kind, tier,
   reason, and spillover fallback where applicable. Preserve existing
   `NoBackendAvailable` and `FeatureUnsupportedError` behavior exactly.

### `engine/inference/router.py`

Keep all placement scopes and fallthrough behavior unchanged. When an acquired
lease is returned, replace its decision with router context:

- physical model: `policy="base"`, `step=None`;
- virtual `route`: `policy="route"`, `step=0`;
- virtual `cascade`: `policy="cascade"`, `step=i`; append
  `cascade_escalation` when `i > 0`.

Do not add a fallback reason merely because an earlier cascade step was
unavailable unless a later step actually acquired the lease.

## Upstream attempts

In `engine/inference/remote/openai_compat.py`:

1. Add `upstream_attempts: int = 0` to `_Outcome`.
2. In `_stream_tokens`, increment `outcome.upstream_attempts` immediately before
   each call to `self._client.stream("POST", ...)`. Leave the bounded retry policy
   untouched.
3. Add read-only `RemoteGenerationStream.upstream_attempts`, returning the shared
   outcome value. This works even though streaming begins after `generate()`
   returns.

Do not add this property to the common stream base class. Serving reads other
stream implementations defensively with `getattr(stream, "upstream_attempts", 0)`.

## Serving lifecycle, metrics, and logs

### `engine/api/serving.py`

Create one internal helper that records a routed terminal outcome. It receives
the lease decision, scheduler wait, stream/result metadata where available, and
the terminal error/cancel state. Use it from both paths below so field meanings
cannot drift:

1. `finish_generation()` after a stream completes, fails, or is cancelled.
2. The synchronous `registry_lease.backend.generate(...)` exception path in
   `start_generation_core()`. That path has already selected a route, so it must
   emit the same decision metric/log with outcome `error`, zero token counts, no
   TTFT/total/output-rate sample, and then release both leases exactly once.

For each routed terminal outcome:

- compute `queue_wait_ms` from `served.lease.waited_s` (or the local scheduler
  lease in the synchronous failure path);
- read `upstream_attempts` defensively;
- call `route_metrics.record(...)` with decision fields and measurements;
- emit exactly one `engine.request` `route_decision` JSON log record with only:
  `request_id`, `endpoint`, `model`, `backend`, `engine`, `tier`, `reason`,
  `fallbacks`, `policy`, `step`, `queue_wait_ms`, `ttft_ms`, `output_tps`,
  `total_ms`, `upstream_attempts`, and `outcome`.

Do not include the exception object or `exc_info` on `route_decision`; existing
error logging remains separate. Do not include prompt text, completions, API
keys, raw headers, remote URL, or request body in this log event.

## Route metrics and `/admin/routes`

Extend `engine/telemetry/route_metrics.py` without changing existing counters or
their semantics:

- add queue-wait sum/count and `avg_queue_wait_ms`;
- add output-rate sum/count and `avg_output_tps`, subject to the valid-sample
  rule above;
- add `upstream_attempts` total;
- retain a stable tier per `(model, backend)` row;
- aggregate `reasons`, `fallbacks`, and `policies` as sorted string-count maps.

`record()` receives `reason`, `fallbacks`, `policy`, `tier`, `queue_wait_ms`, and
`upstream_attempts` in addition to its current arguments. `snapshot()` exposes
the new row fields while retaining all old fields and ordering.

`GET /admin/routes` needs no public-path change; update its docstring and its
end-to-end tests to cover the new snapshot fields.

## Documentation

Update `docs/backends.md` in the multiple-backends/routes section to document:

- every route-decision log and `/admin/routes` field;
- the exact reason and fallback vocabulary;
- that backend name is the current pool identifier and engine kind identifies
  vLLM/SGLang/local/external implementation;
- output-TPS semantics;
- the measurement-only guarantee; and
- the privacy guarantee: no prompts, completions, credentials, or sensitive
  headers are emitted.

## Tests

All tests must be deterministic and require no GPU.

- `tests/inference/test_registry.py`
  - decision reason/tier for normal least-busy, physical single-candidate
    `model_map`, prefix affinity, affinity fallback, and spillover;
  - spillover appends the `spillover` fallback.
- `tests/inference/test_router.py`
  - physical, virtual route, and cascade decisions have correct policy/step;
  - a later cascade step appends `cascade_escalation`;
  - cascade escalation followed by spillover preserves both fallbacks in order.
- `tests/inference/remote/test_remote_backends.py`
  - successful first remote POST records one attempt;
  - one successful pre-stream retry records two;
  - exhausted/non-retriable errors report the actual number of POSTs made.
- `tests/telemetry/test_route_metrics.py`
  - queue-wait and valid output-TPS averages;
  - zero-token, cancelled, errored, and non-positive-duration requests do not
    contribute output-rate samples;
  - attempts total, reasons/fallbacks/policies maps, and tier row field;
  - existing totals and old fields remain compatible.
- `tests/api/test_routes_api.py`
  - `/admin/routes` exposes the enriched fields end to end.
- Serving/API lifecycle tests
  - normal, cancelled, mid-stream-error, and synchronous-`generate()`-error
    paths emit one decision record and account for the route exactly once;
  - prompt and API-key marker strings are absent from both the `LogRecord` extras
    and rendered JSON output, while safe route/latency fields are present.

Run existing registry, router, remote-adapter, serving, route-metrics, routes,
OpenAI, Anthropic, gRPC, spillover, and model-eligibility regressions.

## Verification

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m pytest -q
```

After local checks pass, use the repository’s normal branch/PR process and let
the configured GitHub Actions matrix remain the release-quality source of truth.
For manual validation, send a completion through a two-worker pool, inspect one
safe `route_decision` JSON event, and confirm `/admin/routes` reports the same
backend/engine/tier, reason/fallback breakdown, queue wait, output rate, and
attempt total.

## Out of scope

- Block 12.2b benchmark/load harness and policy comparison gates;
- dashboard live-feed UI for decisions;
- routing-policy changes or automatic rules (Block 12.3);
- dedicated per-engine pool routers (Block 12.4); and
- topology changes (Block 12.5).
