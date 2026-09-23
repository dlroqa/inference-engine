# Operator monitoring aggregations (Block 11, sub-slice 6a)

Read-only rollups that back the operator Clients / Live-Monitoring UI (11.6b).
All are behind the operator gate (loopback dev use or a valid API key) and add no
new persistence — they aggregate data the engine already records.

## Endpoints

### `GET /admin/usage/attribution?window=5h|week`
Per-key usage over the window, busiest first — the same `usage_events` rows that
back quota enforcement, so attribution reconciles with what is enforced. Each row:
`key_id`, `key_prefix`, `key_label`, `client_id` (owning client, if any),
`requests`, `prompt_tokens`, `completion_tokens`, `cu`, `errors` (recorded
responses with a status ≥ 400), and `last_ts`.

### `GET /admin/errors/taxonomy?window=5h|week`
Counts of categorized error records in the window, by taxonomy category
(`validation | auth | limit | model | backend | cancellation | internal`; see
`engine/telemetry/taxonomy.py`). Returns `{categories: {category: count}, total}`.
This captures errors the request edge logs — including pre-generation rejections
(auth/limit) that never record a usage row — complementing the attribution view.

### `GET /admin/alerts`
A computed, read-only operator alert list, most severe first, derived only from
existing signals:

| Alert kind | Source | Severity | Cross-link target |
|------------|--------|----------|-------------------|
| `backend_unhealthy` | a backend is loaded but not ready | critical | `backend` |
| `client_canceled` | a client is canceled (keys revoked) | critical | `client` |
| `client_suspended` | a client is suspended | warning | `client` |
| `webhook_dead_letters` | deliveries in the dead-letter state | warning | `webhook_deliveries` |
| `usage_threshold` | recent `usage.threshold.reached` (per client) | warning | `client` |

Each alert carries `severity`, `kind`, `message`, and a `target_type`/`target_id`
so the UI can link straight to the relevant client, backend, or delivery log.

## Notes

- Single-process, computed on fetch — consistent with the operator event bus;
  cross-node aggregation is a later, scale-time concern.
- No prompts, responses, or secrets are exposed; only structured counts/metadata.

The Clients and Live-Monitoring views that consume these (plus a Security/Audit
view) are sub-slice 11.6b. See
[`BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md`](../BLOCK_11_COMMERCIAL_CONTROLS_INSTRUCTIONS.md).
