# Client account API (Block 11, sub-slice 4)

A small, **read-only** self-service contract a client consumes with their own API
key to see their own account. It is published in the engine's OpenAPI spec
(`/openapi.json`, interactive docs at `/docs`).

## Authentication & isolation

Every `/client/*` endpoint requires a valid, **client-owned** API key
(`Authorization: Bearer sk-ie-…`, or `x-api-key:`). The client identity is
resolved **only** from the key — never from a request field — so a caller can
only ever see their own data. Rejections:

| Case | Status / code |
|------|---------------|
| no key / unknown / revoked-at | `401` `missing_api_key` / `invalid_api_key` |
| key revoked (billing) | `403` `key_revoked` |
| owning client suspended/canceled | `403` `key_suspended` |
| valid key with no owning client (operator/unowned) | `403` `not_a_client_key` |

Unlike the operator surface (`/admin/*`, which allows loopback dev access), these
endpoints always require a real client key.

## Endpoints

- `GET /client/me` — client id, email, status, and current plan summary.
- `GET /client/plan` — the plan entitlement in effect: quota (5h + weekly CU),
  rate limit, allowed models, and whether a plan currently applies (`entitled`).
- `GET /client/usage` — the calling key's `cu_5h` and `cu_weekly` windows
  (`used_cu`, `limit_cu` where 0 = unlimited, `remaining_cu`, `reset_at`) — the
  same figures the gateway enforces — plus `client_weekly_cu`, the aggregate CU
  across all of the client's keys.
- `GET /client/services` — the model names the client may call (all served names,
  narrowed to the plan's `allowed_models` when restricted), and capability flags.

All responses are typed models, so the published OpenAPI is precise. The spec also
declares a `bearerAuth` HTTP security scheme documenting how requests authenticate.

## Related, and not provided

- The client-scoped **event stream** (SSE with `Last-Event-ID` recovery) is
  [`GET /client/events[/stream]`](client-events.md) (Block 11.5).
- Operators manage clients, plans, client keys and webhook endpoints from the
  dashboard's **Clients** view (Block 11.6; administration on `main` for v0.2.0,
  unreleased); see
  [Billing](billing.md) and [Webhooks](webhooks.md).
- **Not provided:** client-initiated mutations. Clients cannot manage their own
  webhook endpoints or change plans through this API; it is read-only.

Block history: [`BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md`](../BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md).
