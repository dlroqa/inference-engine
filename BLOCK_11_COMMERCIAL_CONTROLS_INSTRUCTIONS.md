# Block 11 — Commercial controls (billing lifecycle)

## Purpose

This is the instruction set for Block 11. It may begin only after the serving core
is stable, deployed, and in real operator use, and after Block 3's API-key and
compute-unit (CU) quota foundation is in place.

Block 11 adds **clients** as owners of API keys, **plan/entitlement** records, and
a **single billing-provider (Stripe)** lifecycle that provisions, limits, and
revokes API access. It does not change the public OpenAI or Anthropic API
contracts.

## Preconditions

- API keys exist with display-once creation, SHA-256-only storage, verify, and
  revoke (`engine/auth/keys.py`, migration `0002_auth_quota.sql`).
- Durable per-key usage accounting exists and is the single source of truth for
  both attribution and the CU quota windows (`engine/quota/store.py`,
  `usage_events`).
- Quota is enforced fail-closed on the 5-hour rolling and weekly windows in the
  gateway (`engine/gateway.py`).
- An audit log and a forward-only migration runner exist
  (`engine/store/migrations.py`, head `0005`).
- A short RFC (this document plus the per-sub-slice scope) records the user
  problem, the paid-plan lifecycle, data classification, failure behavior, threat
  model, metrics, and rollback plan.

## Architecture

```text
Stripe (billing provider)
  │  verified, idempotent inbound webhooks
  ▼
Inference Engine gateway
- /billing/webhooks/stripe   (signature-verified, idempotent, retryable)
- clients ── own ──> api_keys
- subscriptions ── reference ──> plans (entitlement: quota, models, rate)
- entitlement resolution: key -> client -> active subscription -> plan
  │
  ▼
Existing auth + quota enforcement
- key status (active/suspended/revoked) checked before quota
- quota windows read PLAN limits (global config is the fallback)
- usage_events remains the metering source of truth
```

The gateway resolves entitlements from stored records only. It never calls the
billing provider on the request hot path, and never trusts webhook content before
verifying its signature.

## Build sequence

### Sub-slice 11.1 — Stripe-first billing lifecycle

- Add `clients`, `plans`, `subscriptions`, and a `webhook_events` idempotency
  ledger (migration `0006_billing.sql`). Add nullable `client_id` and a
  `status` (`active|suspended|revoked`) column to `api_keys` (additive; existing
  keys keep working as unowned keys on the fallback plan).
- Verify the `Stripe-Signature` header over the raw request body **before**
  parsing. Reject invalid signatures with 400 and change no state.
- Record every accepted event in `webhook_events` keyed by the provider event id;
  duplicate deliveries are a 200 no-op (idempotency).
- Handle the minimal lifecycle set — `customer.subscription.created`,
  `customer.subscription.updated`, `customer.subscription.deleted`, and
  `invoice.payment_failed` — mapping them to activate / change-plan / revoke /
  suspend. Process safely and retryably: return 5xx only on transient internal
  error so the provider retries; never on a business no-op.
- Map plan changes to key access, quota (5h + weekly CU), allowed models, and
  rate. Enforce key `status` before the quota check: `suspended` (reversible) and
  `revoked` (terminal) are refused.
- Reconcile metering: confirm the enforcement counters (quota windows) agree with
  `usage_events` per client. No outbound usage events yet.
- Add a `[billing]` config section (provider, webhook signing secret, default
  plan), disabled by default, and document the lifecycle in `docs/billing.md`.

### Sub-slice 11.2 — Outbound usage/webhook delivery

- Standard Webhooks outbound signing, retries/backoff, dead-letter queue,
  delivery log/replay, and secret rotation. Begin only after the accounting model
  is reconciled and auditable (11.1 exit).

### Sub-slice 11.3 — Additional providers, only when justified

- Lemon Squeezy / Paddle only when their business need is confirmed. Reuse the
  provider-neutral entitlement and webhook-idempotency layer from 11.1.

### Sub-slice 11.4 — Client-scoped account contract

- `/client/me`, `/client/usage`, `/client/plan`, `/client/services`, published
  OpenAPI, and strict authorization isolation.

### Sub-slice 11.5 — Client-scoped SSE

- Authenticated stream with `Last-Event-ID` recovery, reconciliation fetch, CORS
  restrictions, and a polling fallback.

### Sub-slice 11.6 — Clients / Live Monitoring UI

- Key-level attribution, error taxonomy, alerts, and cross-links to logs and
  security.

## Do not build (yet)

- Multiple payment providers before one is proven (deferred to 11.3).
- Client-facing realtime portals or self-serve UI before the contract exists
  (11.4–11.6).
- Multi-tenant enterprise hierarchy or organizations (Block 12).
- Automatic collections/dunning workflows.
- Arbitrary outbound webhooks (11.2 defines the bounded set).
- Stripe Checkout/billing-portal embedding on the hot path.

## Required tests

- Valid vs. invalid webhook signature: invalid is rejected with no state change.
- Duplicate webhook delivery is idempotent (second delivery is a no-op).
- Subscription activation, plan change, cancellation, and failed payment each
  update entitlements (access/quota/models) correctly.
- A suspended or revoked key is refused at the gateway before the quota check.
- Usage counters reconcile with `usage_events` per client.
- Unowned/legacy keys fall back to global limits — no Block 3 regression.
- Existing auth, quota, and gateway tests remain green.

## Exit criteria

- A single documented Stripe paid-plan lifecycle reliably provisions a key with
  its plan's limits, suspends it on payment failure, and revokes it on
  cancellation — all via verified, idempotent, retryable webhooks.
- Enforcement counters reconcile with the usage log.
- The public OpenAI and Anthropic API contracts are unchanged.

## Security constraints retained from the original plan

- Never log plaintext secrets, webhook signing secrets, or full provider payloads.
- Verify provider signatures before parsing or acting on any webhook.
- Do not disclose one client's IP address, credentials, usage, or billing state
  to another client.
- Billing egress and provider secrets require explicit allowlisting/config and
  safe handling.
- Destructive lifecycle actions (revoke) are terminal and audited; suspension is
  the reversible default for transient payment failure.

## Completion report

Use the completion reporting template in `VERTICAL_SLICE_BUILD_INSTRUCTIONS.md`
and add the original-plan traceability block. Include the migration version, the
`[billing]` config surface, the webhook endpoint, the handled event set, and the
reconciliation command.
