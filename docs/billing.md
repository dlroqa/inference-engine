# Commercial controls — billing lifecycle (Block 11, sub-slice 1)

The engine can attach API keys to **clients**, give each client a **plan**
(entitlement), and let a single billing provider (**Stripe**) drive the
lifecycle: activate on subscription, suspend on payment failure, revoke on
cancellation. It reuses the existing Block 3 key store and CU quota windows — the
plan just supplies the *limits* the gateway enforces per request.

It is **off by default**. Enable it:

```toml
billing_provider             = "stripe"
stripe_webhook_secret        = "whsec_..."   # Stripe endpoint signing secret
stripe_signature_tolerance_s = 300           # reject signatures older than this
default_plan                 = "free"         # seeded with the engine CU limits
```

## Data model (`0006_billing.sql`)

- `plans` — entitlement definitions: `quota_5h_cu`, `quota_weekly_cu` (0 =
  unlimited), optional `rate_limit_per_min` (NULL = engine default), optional
  `allowed_models` (JSON array; NULL = all models). **Operator-controlled.**
- `clients` — key owners; `external_ref` is the Stripe customer id
  (`cus_...`); `status` is `active | suspended | canceled`.
- `subscriptions` — one row per provider subscription id; its `status` + `plan_id`
  are the current entitlement source for the client.
- `webhook_events` — idempotency ledger, one row per accepted provider event id.
- `api_keys` gains `client_id` (nullable) and `status`
  (`active | suspended | revoked`). Existing keys stay unowned + active, so
  Block 3 behavior is unchanged.

## Entitlement resolution

For each authenticated request the gateway resolves the key's effective access:

1. Key `status = revoked` → `403 key_revoked` (terminal).
2. Owning client `suspended`/`canceled` (or key `suspended`) → `403 key_suspended`
   (reversible).
3. Unowned/legacy key → engine-wide limits (no plan).
4. Otherwise → the client's active-subscription plan, else the `default_plan`.

The plan then supplies the quota windows, an optional per-minute rate override,
and an optional allowed-model set (a disallowed model → `403 model_not_entitled`).
**Entitlement values never come from the webhook payload** — an event selects a
plan by id (`metadata.plan`); an unknown id falls back to the default plan.

## Stripe webhook — `POST /billing/webhooks/stripe`

Public (provider-facing), authenticated by signature, not the operator gate:

1. Verify `Stripe-Signature` over the **raw body before parsing**
   (`t=<unix>,v1=<hmac-sha256>`, constant-time compare, timestamp tolerance).
   Invalid → `400 invalid_signature`, no state change.
2. Record the event id; a duplicate delivery is a `200` no-op (idempotent).
3. Map the event and return `200`. A transient internal failure returns `500`
   (and forgets the event id) so Stripe retries; a business no-op never does.

Handled events:

| Event | Action |
|-------|--------|
| `customer.subscription.created` / `updated` (active/trialing) | activate client, set plan |
| `customer.subscription.updated` (other status) | record plan/status change |
| `invoice.payment_failed` | suspend client (reversible) |
| `customer.subscription.deleted` | cancel client, **revoke** its keys (terminal) |

Any other event type is ignored (no-op success).

## Operator controls (`/admin/billing/*`, behind the operator gate)

- `POST /admin/billing/plans` / `GET /admin/billing/plans` — define/list plans.
- `POST /admin/billing/clients` / `GET /admin/billing/clients` — register/list
  clients (set `external_ref` to the Stripe customer id).
- `POST /admin/billing/clients/{id}/keys` — create a key owned by the client
  (token returned **once**).
- `GET /admin/billing/clients/{id}/reconcile` — per-window CU summed across the
  client's keys.

## Metering reconciliation

`usage_events` remains the single source of truth for both attribution and the CU
quota windows, so a client's enforced usage always reconciles with the log. The
reconcile endpoint (and `BillingStore.client_usage_cu`) sum the same rows the
gateway enforces against.

## Security notes

- Signatures are verified before any parsing/handling; the signing secret is never
  logged, and neither are full payloads (only event id/type/action).
- One client's IP, credentials, usage, or billing state is never exposed to
  another client.
- Suspension is the reversible default for transient payment failure; revocation
  (on cancellation) is terminal and sets `revoked_at`, so the Block 3 verify path
  rejects the token too.

## Deferred (later Block 11 sub-slices)

Outbound usage events / Standard Webhooks (11.2), additional providers (11.3),
the client-scoped `/client/*` contract + OpenAPI (11.4), client SSE (11.5), and
the Clients/Live-Monitoring UI (11.6). See
[`BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md`](../BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md).
