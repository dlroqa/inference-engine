# Client event stream (Block 11, sub-slice 5)

A client can stream its own **account events** — the same billing lifecycle and
usage-threshold events as outbound webhooks — over Server-Sent Events (SSE), with
`Last-Event-ID` recovery and a polling fallback. It is the pull/streaming
complement to [outbound webhooks](webhooks.md): the same events, delivered to a
browser or SDK that holds a connection.

It is **off by default**. Enable it (and, for browser clients on another origin,
allow that origin):

```toml
client_events_enabled                  = true
client_sse_heartbeat_s                 = 15
client_events_retention_max_age_s      = 2592000   # 0 = keep all
client_events_retention_max_per_client = 1000      # 0 = unlimited
client_cors_origins                    = ["https://app.example.com"]  # [] = same-origin only
```

## Event source

Account events are recorded to a durable, ordered, per-client log the moment they
occur. The single emitter fans each event out to **both** channels — this SSE log
and outbound webhooks — so the two never diverge; each channel honors its own
enabled flag. Events are the same envelopes as webhooks (`subscription.activated`,
`subscription.updated`, `subscription.suspended`, `subscription.canceled`,
`usage.threshold.reached`) and carry structured metadata only — never prompts,
responses, or secrets. The log id is the stream cursor.

## Endpoints

Both require a valid, **client-owned** API key and are scoped to the caller only
(the client is resolved from the key; a query param can never widen scope).

- `GET /client/events/stream` — the SSE stream. Because a browser `EventSource`
  cannot set an `Authorization` header, the key may be passed as `?api_key=…`
  (or the usual header for non-browser clients). On connect the server replays
  missed events (`id > Last-Event-ID`), then pushes live events; each frame carries
  `id: <seq>` so the browser tracks the cursor automatically, and heartbeat
  comments (`: keep-alive`) keep the connection open. Resume by reconnecting — the
  browser sends the last `id` it saw as `Last-Event-ID`.
- `GET /client/events?since=<id>&limit=<n>` — reconciliation and **polling
  fallback**: the same events as JSON (`{events, last_id}`) for clients that can't
  hold a connection (proxies) or need to catch up after a gap.

Both `404` when `client_events_enabled` is off.

## Recovery

The stream is resumable and lossless: reconnect with `Last-Event-ID` (or poll with
`since`) and you receive exactly the events with a higher id, in order, then live
events. The log is bounded by the retention settings, so a client that has been
gone longer than retention should reconcile via a full fetch.

## CORS

Cross-origin access is restricted to `client_cors_origins` and applies **only** to
`/client/*` (preflight included); no other route's CORS posture changes. With the
default empty list, the endpoints are same-origin only.

## Security notes

- The key in `?api_key=` can appear in browser history or an intermediary's access
  logs; the engine itself logs the route path, not the query string. Prefer the
  Authorization header for non-browser clients.
- Isolation is enforced from the key: one client can never read another's events.
- Single-process by design — cross-node fan-out (Redis/NATS) is a later, scale-time
  concern, exactly like the operator event bus.

## Deferred

Client-initiated mutations (managing their own webhook endpoints / plan) and the
Clients/Live-Monitoring UI (11.6, which consumes this API). See
[`BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md`](../BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md).
