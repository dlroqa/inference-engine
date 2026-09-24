# Block 12.2b — Reproducible benchmark harness and comparison gate

## Purpose

Block 12.2a records honest per-route terminal measurements. This slice builds
the reproducible, operator-run harness that exercises the engine through its
public OpenAI-compatible HTTP API, reports the Block 12 RFC §8 metric set, and
implements the pure comparison gate that Block 12.3 must clear before a routing
rule can be enabled.

This slice is measurement and evaluation infrastructure only. It must not change
engine routing, model/feature eligibility, scheduler admission, retries,
failover, public API behavior, or default routing policy. Real vLLM/SGLang
numbers remain an operator task on target GPU hardware. CI proves the harness
and gate using fakes and fixtures.

Read these documents before implementing:

- `docs/rfcs/block-12-dual-engine-routing.md`, especially §§5, 8, 9, and 11;
- `BLOCK_12_1_HETEROGENEOUS_MODEL_ELIGIBILITY_INSTRUCTIONS.md`;
- `BLOCK_12_2A_ROUTE_DECISION_OBSERVABILITY_INSTRUCTIONS.md`;
- `engine/telemetry/route_metrics.py` and `docs/backends.md`;
- `scripts/load_test.py` for local conventions only. Do not extend that script.

## Non-negotiable measurement contract

Resolve these rules in code, tests, report schema, and documentation before
adding workload generators or a CLI. Do not silently substitute an available
but semantically different measurement.

### Client timing

- A benchmark request uses `POST /v1/chat/completions` and records client-side,
  monotonic-clock end-to-end timing.
- For streaming requests, **TTFT is the time to the first non-empty
  `choices[].delta.content` value**. It is not time to response headers, the
  opening role-only SSE frame, a keepalive, the terminal frame, or `[DONE]`.
  The current OpenAI edge emits a role-only opening chunk before generation.
- Total client latency is from immediately before sending the request until the
  response completes, the client intentionally cancels, or the transport fails.
- A non-streaming OpenAI response has no client-observable first token. Either
  drive a request as streaming when TTFT is required, while preserving the
  workload's logical shape, or represent its TTFT as unavailable. Never invent
  a TTFT from total latency.
- Keep client measurements distinct from engine-side `ttft_ms` / `total_ms`.
  They have different timing boundaries and must not be merged.

### Token and cost measurements

- `output_tps_sample()` in `engine.telemetry.route_metrics` remains the
  authoritative definition: `completion_tokens / (total_ms / 1000)`, only for
  successful, non-cancelled, token-producing requests with positive duration.
- The current public OpenAI SSE response does **not** expose terminal usage, and
  content chunks are not reliable tokenizer-token counts. The driver must not
  call chunks, words, characters, or bytes "tokens" and must not calculate a
  fake per-request output TPS.
- Obtain token totals, output-TPS, and cost only from an explicitly documented,
  trustworthy source. In the current API that is a before/after delta of the
  operator-gated `/admin/routes` snapshot, not a single cumulative snapshot.
- Such a delta is valid only for an exclusive benchmark target with no unrelated
  traffic for the measurement window. Enforce or prominently require this mode;
  otherwise mark route-derived cost/rate unavailable rather than reporting an
  incorrect value.
- `/admin/routes` is aggregated by `(model, backend)`, not request or workload
  slice. Do not claim exact per-slice route cost/rate from it. Report aggregate
  route deltas separately, or add a deliberately designed, privacy-safe,
  benchmark-scoped measurement interface before claiming that granularity.

### Outcomes and rates

- Outcomes are `ok`, `error`, and `cancelled`; an explicit error wins over
  cancellation, matching Block 12.2a.
- A non-2xx response or transport failure before the first content token is a
  pre-stream failure. A terminal SSE error, disconnect, or abort after a first
  content token is a post-stream failure. The opening role frame does not make a
  failure post-stream.
- An intentional `cancel_after_tokens` close is recorded as client-initiated
  cancellation. Do not mislabel it as a server error. Preserve whether any
  content token was observed.
- Define and document denominators: accepted rate is admitted requests per wall
  second; shed rate is shed requests divided by attempted requests; error and
  pre/post-stream failure rates are errors divided by admitted requests.
  Cancellations are reported separately and do not count as errors or sheds.
- Percentiles include only samples for which that metric is defined. A missing
  or too-small sample set is `null`/unavailable, never `0`.

### Comparability and reproducibility

- A report carries a versioned schema and a serializable workload manifest:
  workload name/version, seed, exact per-slice counts, concurrency schedule,
  warm-up configuration, requested physical/virtual model IDs, logical stream
  mode, max-token limits, and safe workload parameters.
- The manifest contains no prompt text, completion text, session identifier,
  API key, authorization header, or raw request body. For custom workloads,
  store a stable manifest hash and safe descriptive metadata rather than inputs.
- Record harness/package version, `engine.buildinfo.build_info()` output, Python
  version, platform/CPU identity, timestamp, and a sanitized origin. Do not
  shell out to discover Git state: `build_info()["commit"]` may legitimately be
  `null` in a source checkout. Strip URL userinfo, query strings, and fragments;
  never persist credentials.
- Serialization/deserialization is pure. Collect runtime identity at the CLI/
  driver boundary and inject an `IdentityStamp`; `report.py` itself must not read
  the clock, environment, platform, network, or Git.
- A seed makes request selection repeatable, not a GPU comparison statistically
  valid. Support an explicit warm-up phase and fixed measured request count or
  duration. Document repeated target-hardware trials as the operator procedure.

## Package and CLI

Create a new importable package:

```text
engine/bench/
    __init__.py
    __main__.py
    workloads.py
    metrics.py
    gate.py
    report.py
    driver.py
    main.py
```

`__main__.py` must invoke `main.main()` so `python -m engine.bench ...` works.
`main.py` is the argparse implementation; it alone is not sufficient for module
execution.

Add `bench = ["httpx>=0.27"]` to optional dependencies. The driver imports
`httpx` lazily and gives a clear `install .[bench]` error. CI's `.[dev]` already
installs `httpx`, but an operator should not need all development dependencies.

Support at least:

```bash
python -m engine.bench run \
  --base-url https://gateway.example \
  --workload smoke --model chat-8b --out candidate.json

python -m engine.bench compare \
  --baseline baseline.json --candidate candidate.json \
  --target-metric ttft_p95 --target-slice repeated_prefix \
  --min-improvement 0.15
```

Alternatively, every `RequestSpec` may carry a model ID; the selected design
must make the required OpenAI `model` field unambiguous and serialize it in the
manifest. Add authentication options suitable for a non-loopback deployment.
Generation credentials and operator credentials may need to be distinct because
`/admin/routes` is operator-gated. Neither credential may enter a report, log,
or error message.

## Workloads (`workloads.py`)

Keep this module pure. Model a `RequestSpec` and a serializable `Workload`
manifest/configuration. The RFC §5 mix is:

| Slice | Share |
| --- | ---: |
| Unique short prompts | 40% |
| Repeated-prefix prompts | 20% |
| Multi-turn sessions | 15% |
| Long context | 10% |
| Structured output | 10% |
| Cancellation mid-stream | 3% |
| Saturation burst | 2% |

The shares total 100%. Allocate exact request counts deterministically (for
example, largest remainder with a documented tie-break) and expose the resulting
counts in the manifest. A small smoke workload may intentionally use explicit
counts rather than misleading percentage rounding.

Requirements:

- Generate deterministic synthetic prompts from the seed. Do not include them in
  output reports.
- Repeated-prefix cases must share the actual leading characters used by the
  existing prefix-affinity implementation.
- Multi-turn cases model conversation history. They must not claim to test stable
  `session_id` affinity: no public/internal session-ID routing input exists yet.
- Long-context size is configurable and must be compatible with the selected
  model's documented context limit. Do not hard-code a generic unsafe length.
- Structured-output cases include valid `response_format` payloads and run only
  against a model/route configured to support that feature. Unsupported-feature
  responses are correctness failures, not benchmark successes.
- Streaming is an independently recorded transport property. If the driver uses
  streaming to obtain TTFT for a logically non-streaming slice, report that fact
  honestly in the manifest.
- Cancellation cases define `cancel_after_tokens` as observed content chunks for
  client cancellation control, not tokenizer tokens.
- Saturation is a named phase/slice with its own burst concurrency profile, not
  merely ordinary requests with a higher weight.

## Metrics (`metrics.py`)

Keep aggregation pure and fully unit-tested. `RequestSample` should preserve
only safe measurement fields, such as slice/model label, client TTFT/total,
observed content-chunk count, outcome, observed-first-content flag, pre/post
failure classification, and shed flag. It must not store prompts or response
content.

`BenchResult` should include per-slice client metrics and separately labelled
aggregate route-snapshot deltas when they are available. Report metrics by
workload slice and requested model/virtual-model; retain backend/pool dimensions
where a trustworthy source provides them. Do not manufacture per-slice backend
attribution from `/admin/routes`.

Use the existing `scripts/load_test.py::_pct` nearest-rank-compatible behavior
only after documenting it and testing its empty/singleton/boundary cases. Include
TTFT and total-latency mean/p50/p95/p99 when defined, accepted rate, attempted/
admitted/shed/error/cancel counts, failure splits, and route-derived token/cost
metrics only when valid under the measurement contract.

## Report (`report.py`)

Define a versioned `RunReport` with pure `to_dict`/`from_dict`/JSON round-trip
operations. It must include the identity stamp, workload manifest, results, and
explicit availability/source metadata for every metric that cannot be derived
from client samples. Reject malformed or unknown-incompatible schema versions.

Do not include raw endpoint credentials, request inputs, response content, or
unbounded per-request samples by default. If diagnostic samples are ever added,
they must remain opt-in, bounded, redacted, and outside the gate input.

## Comparison gate (`gate.py`)

This is a pure module and the CI-testable policy gate. It implements RFC §8.1
and §8.2 exactly:

- §8.1: candidate improves the declared target metric on the declared target
  slice by at least the requested threshold (default `0.15`).
- §8.2 vetoes a candidate that increases overall error rate by more than 0.5
  percentage points, p95 end-to-end latency by more than 5% on any **other**
  workload slice, or saturation-slice shed rate by more than 2 percentage points.

Requirements:

- Validate metric direction explicitly: lower is better for TTFT, latency, and
  cost; higher is better for accepted request rate and output TPS. Reject unknown
  or unavailable target metrics.
- Reject incomparable reports before calculating a pass: incompatible schema or
  harness version, workload manifest/seed/count/schedule mismatch, missing
  target or required veto slice, mismatched metric source, or an invalid baseline
  denominator. Explain each rejection in `GateOutcome.reasons`.
- Treat zero baselines and undefined percentage changes deliberately; do not
  divide by zero or quietly pass. Rate metrics expressed as percentage points use
  absolute point deltas, not relative percentages.
- Apply the latency veto to every non-target slice. The target slice is excluded
  only where RFC wording says “any other workload slice”; still report its values.
- Equal baseline/candidate values fail a default 15% improvement target. Test
  equality only with an explicit zero threshold if that behavior is desired.
- `GateOutcome` includes `passed`, deterministic human-readable reasons, and
  enough safe values to audit the arithmetic. The CLI exits 0 only on `passed`.

## HTTP driver (`driver.py`)

Keep this thin and async. It fires the generated workload using `httpx`, honors
the concurrency schedule, parses OpenAI SSE correctly, records client samples,
and obtains optional before/after operator snapshots.

- Use a monotonic clock around each request and never block the event loop.
- Consume enough SSE framing to distinguish role-only frames, content frames,
  terminal errors, and `[DONE]`.
- On cancellation, close the response stream immediately after the configured
  number of observed content chunks and allow the server time to release its
  work before final route snapshots/assertions.
- Classify HTTP 429 saturation as shed; preserve non-429 status/transport errors
  as failures without exposing response bodies or secrets in reports.
- Fetch `/admin/routes` and `/metrics` only when credentials/availability permit.
  A failed optional operator snapshot must make the relevant metrics unavailable,
  not fail an otherwise valid client-only run unless the selected workload/gate
  explicitly requires those metrics.
- Never assume a cumulative admin value belongs solely to this run; calculate and
  label only a before/after delta under the exclusive-target requirement.

## Tests

All tests are deterministic and require no GPU.

- `tests/bench/test_workloads.py`: seed reproducibility; exact allocation and
  total shares; every declared shape; safe, well-formed structured/long/cancel
  specs; no claim of unsupported session-ID affinity.
- `tests/bench/test_metrics.py`: percentile boundaries and unavailable values;
  accepted/shed/error/cancellation denominators; content-frame TTFT semantics;
  pre/post-stream classification; route-derived metrics only when supplied as a
  valid aggregate source.
- `tests/bench/test_gate.py`: target threshold pass/fail boundary for both metric
  directions; equality fails at the default threshold; every RFC §8.2 veto;
  invalid/missing/zero-baseline data; manifest incompatibility; deterministic
  reasons.
- `tests/bench/test_report.py`: pure JSON round-trip; schema validation; required
  identity fields; sanitizer/privacy tests proving prompt and credential marker
  strings do not appear.
- `tests/bench/test_driver_e2e.py`: use `running_app` and a real Uvicorn server
  with `FakeBackend`, not in-process ASGI transport. Configure sufficiently many
  delayed fake tokens for cancellation to occur before natural completion. Assert
  first-content TTFT, sane total timing/outcomes, cancellation cleanup, safe
  report contents, and optional route-snapshot delta behavior. Configure the fake
  and settings correctly for any structured-output case. Skip cleanly only when
  `httpx` is unavailable.
- `tests/bench/test_cli.py`: invoke `python -m engine.bench`; verify module entry
  point, zero exit on a genuine improvement, and non-zero exit on each comparison
  failure. Do not start a live server merely to test pure `compare` behavior.

Prefer covering the driver and CLI. Do not add a coverage omit preemptively; only
add a narrow, justified omit after measuring coverage and only for irreducible
process-entry glue.

## Documentation and verification

Add `docs/benchmarks.md`, linked from `docs/backends.md` and RFC §§9/11. It must
document the workload manifest, client versus route-derived metric definitions,
SSE TTFT rule, unavailable-metric behavior, failure/cancellation denominators,
exclusive-target requirement, gate arithmetic, safe report contents, exact run
and compare commands, and the CI-fakes versus operator-GPU boundary.

Keep RFC §9 as a template until an operator records real target-hardware runs;
then paste the safe command, hardware/config identity, and measured values.

Run:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m pytest -q
```

The pre-12.2b suite currently collects 488 tests. Do not rely on a stale 487
baseline. Use the repository's normal branch/PR process; GitHub Actions remains
the release-quality source of truth.

## Out of scope (later)

- Enabling any routing rule, including using this gate to authorize a real rule
  (Block 12.3).
- Dashboard UI for benchmark results.
- A public session-ID affinity API; multi-turn requests alone do not create it.
- Per-engine pool routers (Block 12.4).
- Topology evolution (Block 12.5).
- Committing real GPU baseline numbers. That is an operator task; RFC §9 remains
  templated, with the exact safe commands and required hardware/configuration
  identity documented so the operator can fill it in.
