# Benchmark harness & comparison gate (Block 12.2b)

The harness drives the engine through its **public OpenAI-compatible API**, reports
the Block 12 RFC §8 metric set from honest client-side measurements (plus optional
aggregate route deltas), and implements the pure **§8.1/§8.2 comparison gate** that
Block 12.3 must clear before a routing rule may be enabled. It is
measurement/evaluation only — it changes no engine behaviour. Real vLLM/SGLang
numbers are an operator task on target GPU hardware; CI proves the harness and gate
against fakes and fixtures (see `tests/bench/`).

Package: `engine/bench/` (`python -m engine.bench`). Install the driver extra to
run against a live engine:

```bash
pip install -e '.[bench]'     # adds httpx; the pure `compare` gate needs no extra
```

## Commands

```bash
# Produce a baseline, then a candidate, against a live gateway (operator/GPU):
python -m engine.bench run --base-url https://gateway.example \
    --workload standard --model chat-8b --exclusive-target \
    --admin-api-key "$OP_KEY" --api-key "$GEN_KEY" --out baseline.json

# ... change config / enable a candidate policy, run again --out candidate.json ...

# Gate the candidate against the baseline (pure; runs anywhere, exits non-zero on fail):
python -m engine.bench compare --baseline baseline.json --candidate candidate.json \
    --target-metric ttft_p95 --target-slice repeated_prefix --min-improvement 0.15
```

`--api-key` (generation) and `--admin-api-key` (operator, for `/admin/routes`) may
differ; **neither is ever written to a report, log, or error message**.

## Workloads

`--workload standard` expands the RFC §5 mix (unique 40%, repeated-prefix 20%,
multi-turn 15%, long-context 10%, structured 10%, cancellation 3%, saturation 2%)
into exact per-slice counts by largest-remainder allocation (tie-break: larger
remainder first, then declaration order). `--workload smoke` uses tiny explicit
counts. Prompts are synthesised deterministically from `--seed` and are **never**
serialised into a report — only the safe manifest is (counts, concurrency schedule,
warm-up, model, limits, shape parameters).

Notes:

- **Repeated-prefix** requests share the exact leading `--prefix-chars` characters
  (what the prefix-affinity implementation keys on).
- **Multi-turn** models conversation *history* only. It does **not** exercise
  session-ID affinity — no such routing input exists yet.
- **Long-context** size (`--long-context-chars`) must fit the model's context.
- **Structured** requests send `response_format` and must run only against a model/
  route that supports it (`--structured`); an unsupported-feature response is a
  correctness failure, not a benchmark success.
- **Saturation** is a named phase driven at `--burst-concurrency`, not ordinary
  requests with a higher weight.

## Metric definitions

Client-side (always available):

- **TTFT** — monotonic client time to the **first non-empty
  `choices[].delta.content`**. The role-only opening SSE frame, keepalives, the
  terminal frame, and `[DONE]` do **not** count. Kept distinct from the engine-side
  `ttft_ms` in `/admin/routes` (different boundaries).
- **Total latency** — from just before sending until the response completes, the
  client cancels, or the transport fails. Latency percentiles use successful,
  token-producing requests; TTFT percentiles use requests that produced a first
  content token. Undefined percentiles are `null`, never `0`.
- **Denominators** — `accepted_rate` = admitted / wall-second; `shed_rate` = shed /
  attempted; `error_rate` and pre/post-stream failure rates = count / admitted.
  Cancellations are reported separately and are never errors or sheds.
- **Outcomes** — `ok`, `error`, `cancelled` (an explicit error wins over
  cancellation). A non-2xx/transport failure before the first content token is
  **pre-stream**; an SSE error/abort after a first content token is **post-stream**.
  A `429` is classified as **shed**, not a failure.

Route-derived (optional, aggregate only):

- Token totals, output-TPS, and cost are **not** computed from client SSE content
  chunks (chunks are not tokenizer tokens; the public SSE exposes no terminal
  usage). They come only from a **before/after delta of the operator-gated
  `/admin/routes` snapshot**, and only under an **exclusive-target** run (no other
  traffic for the window). `/admin/routes` is aggregated by `(model, backend)`, so
  the harness reports an **aggregate** route delta, never per-slice route cost. If
  the snapshot is unavailable, the relevant metrics are marked unavailable rather
  than guessed.

## The comparison gate

- **§8.1** — the candidate must improve the declared `--target-metric` on the
  declared `--target-slice` by at least `--min-improvement` (default 0.15),
  direction-aware (lower is better for TTFT/latency/cost; higher for accepted rate /
  output-TPS). Equal values fail the default threshold.
- **§8.2** — the candidate is vetoed if it raises overall error rate by more than
  0.5 percentage points, p95 end-to-end latency by more than 5% on any **other**
  slice, or the saturation slice's shed rate by more than 2 percentage points.
- **Reject-first** — incomparable reports (schema/harness mismatch, workload
  manifest/seed/count/schedule mismatch, missing target, mismatched route-metric
  source or a non-exclusive route-derived run, invalid/zero baseline denominator)
  are rejected with a deterministic reason before any pass is computed. Rate deltas
  use absolute percentage points; latency uses relative percent.

The CLI exits `0` only when the gate passes, `1` on a gate failure, and `2` on a
malformed report.

## CI vs. operator hardware

CI exercises the workload generators, metric aggregation, gate arithmetic, report
round-trip/privacy, and one end-to-end driver run against a real Uvicorn server with
a `FakeBackend` — no GPU. Real target-hardware numbers (RFC §9) are produced by an
operator running `python -m engine.bench run` against real vLLM/SGLang, with an
explicit warm-up and repeated trials; a seed makes request *selection* repeatable
but does not make a single GPU comparison statistically valid.

RFC §9 stays a template until an operator records real runs; then paste the safe
command, hardware/config identity, and measured values.
