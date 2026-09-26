# Operability (Block 4)

This document defines the operability surfaces, the exact meaning of each metric,
and — just as importantly — their limitations. The goal is that an operator can
tell **whether a failure originated in client input, auth/limits, model state, or
backend execution**, and can read health/load without any secret ever leaking.

## Surfaces

| Surface | Kind | Purpose |
|---|---|---|
| `WS /ws/metrics` | WebSocket | Periodic metrics snapshots; one snapshot is pushed immediately on connect, then on each sampler tick. |
| `WS /ws/feed` | WebSocket | Inference live feed events; a bounded ring buffer of recent events is replayed to a late joiner before live events. |
| `GET /metrics` | REST | The current metrics snapshot (fallback for `/ws/metrics`). |
| `GET /logs` | REST | Recent structured `log_events` (query params: `limit`, `level`, `request_id`). |
| `GET /diagnostics` | REST | A redacted support bundle: config + hardware summary + current metrics + recent logs. |

### Access control

All five surfaces (and every `/admin/*` endpoint) use one operator gate. An
operator authenticates with an **operator** API key via `Authorization: Bearer …`,
`x-api-key: …`, or — for WebSockets — a `?api_key=…` query parameter. Credentials
are checked **before** the loopback exception:

| Credentials presented | Operator surface |
|---|---|
| Valid operator key | Allowed |
| Valid **client-owned** key (billing client), from any host incl. localhost | `403 operator_role_required` |
| Invalid or revoked key, from any host incl. localhost | `401 operator_access_required` |
| No key while effective auth is **on** (`require_auth`, or any non-loopback bind) | `401`, even from localhost |
| No key while effective auth is **off** | Allowed for loopback development use only |

Keys owned by a billing client (`/admin/billing/clients/{id}/keys`) keep working
for inference and `/client/*`, but never open operator surfaces. Rejected
WebSocket handshakes are closed with code `1008` before the socket is accepted.
Operator RBAC/SSO beyond the operator/client split is later work.

## Metrics snapshot

```jsonc
{
  "ts": 1_700_000_000.0,          // wall-clock seconds when sampled
  "uptime_s": 123.4,               // seconds since the app started
  "counters": {
    "requests_total": 10,          // cumulative requests admitted to serving
    "requests_active": 1,          // in-flight right now (gauge)
    "requests_errors": 2,          // cumulative failed requests
    "prompt_tokens_total": 900,    // cumulative prompt tokens
    "completion_tokens_total": 400,// cumulative completion tokens
    "requests_per_min": 3          // starts in the last rolling 60 seconds
  },
  "throughput": { "completion_tokens_per_s": 12.5 },  // measured between samples; null until 2 samples
  "resources": {
    "available": true,             // false if psutil is unavailable
    "cpu_percent": 18.0,           // system-wide, since the previous sample
    "memory": { "total": …, "used": …, "available": …, "percent": … },
    "process_rss": 84_213_760,     // this process's resident memory (bytes)
    "disk": { "path": …, "total": …, "used": …, "free": …, "percent": … }  // of the data dir
  },
  "gpu": { "available": false, "reason": "no tested GPU probe in this build" },
  "energy": { "state": "unavailable", "watts": null, "j_per_token": null, "tokens_per_joule": null, "source": null, "reason": … },
  "backend": { "state": "ready", "model_id": "…", "available": true },
  "scheduler": { "in_use": …, "queue_depth": …, "admitted_total": …, "rejected_total": …, … }  // Block 7; null if no scheduler
}
```

The `scheduler` panel (admission control) is defined in full — every field and the
saturation/backpressure behavior behind it — in
[docs/concurrency.md](concurrency.md).

### Definitions & limitations

- **Counters** are process-local and reset on restart. `requests_total` counts
  requests **admitted to serving** (past auth/limits/quota); requests rejected at
  the gate are not counted here but are logged (see the error taxonomy). `requests_active`
  is decremented exactly once per admitted request, including on error/cancel.
- **`requests_per_min`** is a rolling count of request *starts* in the last 60 s,
  not a smoothed rate.
- **`throughput.completion_tokens_per_s`** is derived from the change in
  `completion_tokens_total` between two consecutive samples, so it is `null` on the
  first sample and is an engine-wide average over the sample interval — not a
  per-request instantaneous rate.
- **Resources** come from `psutil`. `cpu_percent` is system-wide and relative to
  the previous sample (the first sample after start may read low). `disk` reports
  the filesystem containing the data directory. If `psutil` is unavailable, the
  block reports `available: false` and numeric fields are `null` — the stream
  never fails because a probe is missing.
- **GPU** is always reported as `available: false` in this build: no GPU probe has
  been tested here, and an untested probe must not be presented as real data.
- **Energy** is reported as `measured` **only** where a validated power probe is
  readable. The only probe in this block is Intel/AMD **RAPL** via the Linux
  `powercap` sysfs interface; power (W) is derived from the counter delta over the
  sample interval, and energy-per-token from measured watts and measured token
  throughput. Where RAPL is absent, unreadable (permissions), non-Linux, or a
  counter wrap is detected, the state is `unavailable` with a reason. **TDP-derived
  estimates are never presented as measured energy.** Cloud CI runners typically
  restrict RAPL, so `unavailable` there is expected and correct.

## Live feed events

Each event is a JSON object with a `type` and `ts` plus structured fields. No
prompt or response text is ever included.

| `type` | Fields |
|---|---|
| `request.start` | `request_id`, `endpoint`, `model`, `key_id` |
| `request.progress` | `request_id`, `tokens` (emitted so far; throttled to ≤1 / 250 ms per request) |
| `request.end` | `request_id`, `model`, `prompt_tokens`, `completion_tokens`, `finish_reason`, `ttft_ms`, `total_ms` |
| `request.error` | `request_id`, `category`, `endpoint`, `model` |

The feed is bounded two ways: a per-channel recent-event **ring buffer**
(`event_history_size`) replayed to late joiners, and a per-subscriber **queue**
(`event_subscriber_queue`) whose oldest event is dropped if a client falls behind —
so one slow consumer can never block publishers or grow memory without limit.

## Error taxonomy

Every failure is classified into exactly one category so responsibility is
unambiguous:

| Category | Meaning | Typical status |
|---|---|---|
| `validation` | Malformed/unsupported client input | 400 |
| `auth` | Missing/invalid credentials | 401 |
| `limit` | Rate, concurrency, body-size, or quota rejection | 413 / 429 |
| `model` | No model loaded / model not found / not ready | 404 / 503 |
| `backend` | Inference backend execution failure | 500 / 503 |
| `cancellation` | Client disconnected / consumer aborted | — |
| `internal` | Unexpected server error | 500 |

Error responses carry an `x-request-id` correlation header. A failed request also
writes a structured row to the bounded `log_events` table with its category, the
lifecycle **stage** it failed at, the route, the request id, and — for backend
failures — a **stacktrace**. This is what makes the Logs view and
`GET /logs?request_id=…` a real troubleshooting tool.

## Privacy

By construction, none of these surfaces expose prompts, responses, or secrets:

- Metrics are aggregate counters and host resource figures.
- Log records store only structured metadata (level, ids, route, model, key id,
  category, stage, a redacted message, and stacktraces). The engine never logs
  prompt/response content.
- The diagnostics bundle passes configuration through a redactor that masks any
  secret-named field (`*secret*`, `*token*`, `*password*`, `*api_key*`, …). API
  keys are stored only as hashes and are never recoverable, so no live secret
  exists to leak; the redactor keeps that true as configuration grows.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `metrics_interval_s` | `1.0` | Sampler tick interval in seconds; `0` disables the sampler (immediate snapshots still work). |
| `event_history_size` | `200` | Recent events kept per channel for late-joiner replay. |
| `event_subscriber_queue` | `500` | Per-subscriber queue depth before dropping oldest. |
| `log_ring_size` | `500` | In-memory recent-log tail size (all levels). |
| `log_events_max_rows` | `2000` | Row cap for the persisted `log_events` table (WARNING+). |
