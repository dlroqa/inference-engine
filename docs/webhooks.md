# Outbound webhooks (Block 11, sub-slice 2)

The engine can **notify clients** of events over HTTP using the
[Standard Webhooks](https://www.standardwebhooks.com/) format: signed deliveries,
automatic retries with backoff, a dead-letter state, a replayable delivery log,
and secret rotation. This is the outbound counterpart to the inbound Stripe
webhook in [billing.md](billing.md).

It is **off by default**. Enable it and set an egress allowlist:

```toml
webhooks_enabled                = true
egress_allowlist                = ["hooks.example.com"]   # endpoint hosts must match
webhook_max_attempts            = 5
webhook_backoff_schedule_s      = [30, 120, 600, 1800]
webhook_poll_interval_s         = 5
webhook_delivery_timeout_s      = 10
webhook_signing_rotation_grace_s = 86400
webhook_usage_threshold_pct     = 0.8   # 0 = off
```

## Events

| Event type | When |
|------------|------|
| `subscription.activated` | Stripe subscription created/updated to active |
| `subscription.updated`   | plan/status change (non-activating) |
| `subscription.suspended` | `invoice.payment_failed` |
| `subscription.canceled`  | subscription deleted |
| `usage.threshold.reached`| a client crosses `webhook_usage_threshold_pct` of its plan's **weekly** quota |

The body is a Standard Webhooks envelope:

```json
{ "id": "evt_…", "type": "subscription.activated",
  "timestamp": "2026-09-23T…Z", "data": { "client_id": "…", … } }
```

Lifecycle events are emitted from the inbound Stripe handler after it applies the
change, so outbound state always mirrors stored billing state. The usage event
uses a deterministic id per client/week/threshold, so it fires **at most once per
weekly window** even under continuous traffic (per-request usage streaming is out
of scope for this slice).

## Delivery, retries & dead-letter

Each event fans out to every enabled endpoint the client has that subscribes to
the type (one `webhook_deliveries` row per endpoint; `UNIQUE(endpoint_id,
event_id)` makes fan-out and re-emission idempotent). A background worker POSTs
due deliveries:

- `2xx` → `succeeded`.
- any other status or a network error → retry, scheduled at
  `now + webhook_backoff_schedule_s[attempt]` (the last delay repeats).
- after `webhook_max_attempts` failures → `dead` (dead-letter).

The worker runs off the event loop (a worker thread), so a slow endpoint never
stalls request serving.

## Signature verification (receiver side)

Headers: `webhook-id`, `webhook-timestamp`, `webhook-signature`. Compute
`base64(HMAC_SHA256(secret_bytes, f"{webhook-id}.{webhook-timestamp}.{body}"))`
and constant-time compare it to a `v1,<sig>` token in `webhook-signature`. The
secret is `whsec_<base64>`; `secret_bytes` is the base64-decoded portion. During
rotation the header carries several space-separated `v1` tokens — accept if any
one matches.

## Secret rotation

`POST /admin/billing/webhooks/endpoints/{id}/rotate-secret` adds a new secret and
expires the current one after `webhook_signing_rotation_grace_s`. During the grace
window deliveries are signed with **both** secrets, so a receiver still using the
old secret keeps verifying until it switches to the new one.

## Operator API (behind the operator gate)

- `POST /admin/billing/webhooks/endpoints` — register `{client_id, url,
  description?, event_types?}` (URL host must be egress-allowlisted; secret
  returned **once**).
- `GET /admin/billing/webhooks/endpoints[?client_id=…]` — list.
- `POST /admin/billing/webhooks/endpoints/{id}/disable?disabled=true|false`.
- `DELETE /admin/billing/webhooks/endpoints/{id}`.
- `POST /admin/billing/webhooks/endpoints/{id}/rotate-secret`.
- `GET /admin/billing/webhooks/deliveries[?endpoint_id=&status=&limit=]` — the
  delivery log.
- `POST /admin/billing/webhooks/deliveries/{id}/replay` — re-enqueue (e.g. a
  dead-lettered delivery after the endpoint is fixed).

## Security notes

- Destination hosts are constrained by `egress_allowlist`; only `http(s)` URLs are
  accepted.
- Signing secrets are shown once and never logged; delivery logs record status
  codes and the engine's own error strings, never bodies or secrets.
- A webhook failure never affects the request or lifecycle action that produced
  the event (emission is best-effort and isolated).

## Deferred (later Block 11 sub-slices)

Client-scoped self-serve endpoint management and the `/client/*` contract (11.4),
client SSE (11.5), and the Clients/Live-Monitoring UI (11.6). See
[`BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md`](../BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md).
