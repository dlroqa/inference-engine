# Controlled concurrency (Block 7)

This document defines how the engine behaves under concurrent load: how many
generations run at once, what happens to overlapping requests, and how the
behavior is made observable. The goal is that the engine **fails predictably and
recoverably under load** — it sheds excess work with a clear, retriable signal
rather than degrading silently or exhausting memory.

## The honest concurrency policy

The Tier-1 runtime is `llama.cpp`, whose single model context **serializes
decoding** — one generation advances at a time. The engine does not pretend
otherwise. The default `max_concurrency` is therefore **1**: one generation runs
while others wait. Continuous batching, arbitrary `parallel` slots, and
prefill/decode disaggregation are explicitly *not* implemented here (they are
later, benchmark-backed work), so the engine never advertises parallelism it
cannot deliver.

The value of admission control at this tier is not batch packing — it is:

- keeping the event loop responsive (streaming tokens, serving the dashboard and
  health/metrics) while a generation runs in a worker thread, and
- absorbing bursts of overlapping requests **gracefully** (queue them, run them in
  order) instead of rejecting them outright, while still protecting the process
  from unbounded queueing.

`max_concurrency` is a knob, not a promise: raising it above 1 only helps on a
backend that can actually run generations in parallel. On CPU it also risks
contention (few physical cores), so raise it deliberately and measure.

## What happens to a request

```
                         ┌─ slot free ──────────────► run immediately
request ─► authenticate ─┤
          + rate/quota   └─ all slots busy ─► queue (bounded) ─┬─ slot frees ─► run
                                                               ├─ queue full ─► 429 engine_saturated
                                                               └─ wait > timeout ─► 429 engine_saturated
```

1. **Authentication, rate, and quota** checks run first (Block 3), unchanged.
2. **Admission**: the request acquires one of `max_concurrency` slots.
   - A free slot is taken immediately.
   - Otherwise the request waits in a queue of at most `max_concurrency_queue`
     waiters, for at most `concurrency_queue_timeout_s` seconds.
3. **Saturation**: if the queue is already full, or the wait times out, the
   request is rejected — it does **not** block indefinitely or grow memory.

### Saturation response

```jsonc
HTTP/1.1 429 Too Many Requests
retry-after: <seconds>
{
  "error": {
    "message": "the engine is at capacity; please retry shortly",
    "type": "rate_limit_error",
    "code": "engine_saturated",
    "param": null
  }
}
```

`429` with a `retry-after` header is the standard "back off and retry" signal, so
OpenAI-compatible SDK clients retry it automatically with backoff. The
`retry-after` hint is derived from the observed average queue wait (floored at one
second).

## Releasing capacity

A slot is held for exactly the lifetime of one generation and released when it
ends — on normal completion, on backend failure, and on **client disconnect**. A
disconnected streaming client aborts its generation and frees the slot promptly
(the worker thread is cancelled and joined), so a client that goes away never ties
up capacity. Cancellations are counted so this is visible.

## Backpressure on slow consumers

Tokens flow from the worker thread to the client through a **bounded** queue
guarded by a semaphore. If a client consumes tokens slower than the model produces
them, the producer blocks on the full queue rather than buffering the whole
response in memory — so a slow consumer **cannot grow the token buffer without
bound**. Each time this happens the request is flagged, and the engine increments a
`slow_consumer_total` counter so the condition is observable.

## Observability

The metrics snapshot (`GET /metrics`, `WS /ws/metrics`) carries a `scheduler`
panel:

```jsonc
"scheduler": {
  "max_concurrency": 1,          // configured slots
  "max_queue_depth": 32,         // configured queue capacity
  "queue_timeout_s": 30.0,       // configured wait ceiling
  "in_use": 1,                   // slots currently occupied
  "available": 0,                // slots free right now
  "queue_depth": 3,              // requests currently waiting
  "peak_in_use": 1,              // high-water mark of occupied slots
  "peak_queue_depth": 7,         // high-water mark of waiters
  "admitted_total": 128,         // requests granted a slot
  "rejected_total": 4,           // requests shed (sum of the two below)
  "rejected_queue_full": 3,      // shed because the queue was full
  "rejected_timeout": 1,         // shed because the wait timed out
  "cancelled_total": 5,          // generations cancelled (e.g. client disconnect)
  "slow_consumer_total": 2,      // requests that hit token-buffer backpressure
  "wait_ms_avg": 40.2,           // mean time waited for a slot (null if none)
  "wait_ms_max": 210.5,          // longest wait for a slot
  "wait_ms_last": 12.0           // most recent wait for a slot
}
```

The live feed (`WS /ws/feed`) emits a `request.rejected` event carrying the
`reason` (`queue_full` or `queue_timeout`) and the `retry_after_s` hint whenever a
request is shed.

## Configuration

| Key | Env | Default | Meaning |
|---|---|---|---|
| `max_concurrency` | `IE_MAX_CONCURRENCY` | `1` | Generations allowed to run at once. |
| `max_concurrency_queue` | `IE_MAX_CONCURRENCY_QUEUE` | `32` | Waiting requests allowed (`0` = shed immediately when busy). |
| `concurrency_queue_timeout_s` | `IE_CONCURRENCY_QUEUE_TIMEOUT_S` | `30.0` | Max seconds a request waits for a slot (`0` = wait until a slot frees or the client disconnects). |

## Load testing

`scripts/load_test.py` drives a running engine with a configurable number of
concurrent clients and prints an admission/latency summary (throughput, p50/p95/p99
latency, and the scheduler counters above). Because the reference sandbox CPU lacks
AVX and cannot run a real GGUF model (see the README), the repeatable profile is
run in CI on GitHub's AVX2 runners against the pinned SmolLM2-135M model; run it
locally against any running engine with:

```bash
python scripts/load_test.py --base-url http://127.0.0.1:8000 \
    --concurrency 16 --requests 128 --max-tokens 32
```

The exit criterion for this block is that under supported concurrent load the
engine keeps health and operator metrics responsive, bounds its queue and memory,
and sheds excess load with `429 engine_saturated` — rather than hanging, crashing,
or exhausting memory.
