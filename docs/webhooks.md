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

**Shutdown and at-least-once delivery.** On shutdown the worker is asked to
stop: the batch finishes the delivery it is on and records its outcome, and the
remaining due deliveries stay `pending`, unsent, for the next start.
`webhook_delivery_timeout_s` limits each network operation, not how long that
takes. Delivery is at least once, not exactly once: if the process is killed
after a receiver accepted a delivery but before its success was recorded, the
delivery is sent again after restart. Each delivery keeps the same
`webhook-id` across attempts, so receivers should deduplicate on it.

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
old secret keeps verifying until it switches to the new one. The response
reports the new secret (once), `grace_s`, and `previous_secret_expires_at` (Unix
seconds), the moment the previous secret stops signing.

## Operator API (behind the operator gate)

- `POST /admin/billing/webhooks/endpoints` — register `{client_id, url,
  description?, event_types?}` (URL host must be ASCII and egress-allowlisted;
  secret returned **once**). A non-ASCII host is refused with
  `endpoint_host_not_ascii`: register the `xn--` (punycode) form instead.
- `GET /admin/billing/webhooks/endpoints[?client_id=…]` — list.
- `POST /admin/billing/webhooks/endpoints/{id}/disable?disabled=true|false`. A
  disabled endpoint gets no new events, and deliveries already queued for it are
  dead-lettered without being sent; enabling it again does not resend them (replay
  them).
- `DELETE /admin/billing/webhooks/endpoints/{id}` — also removes the endpoint's
  signing secrets and delivery history.
- `POST /admin/billing/webhooks/endpoints/{id}/rotate-secret`.
- `GET /admin/billing/webhooks/deliveries[?endpoint_id=&status=&limit=]` — the
  delivery log.
- `POST /admin/billing/webhooks/deliveries/{id}/replay` — re-enqueue (e.g. a
  dead-lettered delivery after the endpoint is fixed). Delivery statuses are
  `pending`, `succeeded` and `dead`.

Every write is recorded in the audit log (`webhook.endpoint.create`, `.disable`,
`.enable`, `.rotate_secret`, `.delete`, `webhook.delivery.replay`) with the
endpoint's host, never its full URL (a path or query may hold a receiver's
credentials) and never a secret. The dashboard's **Clients** view drives all of
these from a client's detail panel.

## Security notes

- Destination hosts are constrained by `egress_allowlist`; only `http(s)` URLs are
  accepted.
- **Redirects are never followed.** The allowlist is checked against the
  registered URL only, so a 3xx response is a failed attempt (retried, then
  dead-lettered) rather than a request to an unchecked host carrying the
  webhook headers.
- **Hosts must be ASCII.** The allowlist check and the connection then see the
  same name, and delivery never goes through the IDNA 2003 (`idna` codec)
  conversion. Endpoints registered before this rule keep working unchanged.
- The allowlist is checked when an endpoint is registered. Narrowing
  `egress_allowlist` later does not disable endpoints that are already
  registered: review and delete them.
- Signing secrets are shown once and never logged; delivery logs record status
  codes and the engine's own error strings, never bodies or secrets.
- A webhook failure never affects the request or lifecycle action that produced
  the event (emission is best-effort and isolated).

## Not provided

Client-scoped self-serve endpoint management: endpoints are operator-managed.
