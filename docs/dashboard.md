# Operator dashboard: wiring explanations

The dashboard explains what each control does and which engine call it makes
through "How this works" popovers (`dashboard/src/components/WiredTo.tsx`).
This page describes how that content is sourced and checked, and ends with the
generated **UI → subsystem → endpoint** table (see
[UI → subsystem → endpoint table](#ui--subsystem--endpoint-table)).

## Sources of truth

```
engine routes → scripts/export_routes.py → dashboard/src/lib/routes.generated.json
wiring.ts + routes.generated.json → WiredTo.tsx → views
```

- `dashboard/src/lib/wiring.ts` holds every explanation: labels, plain-language
  descriptions, subsystem chains, endpoints, audit facts, kill-switch names and
  docs references (`WIRING`), plus controls that make no direct engine call
  (`LOCAL_CONTROLS`). Views never hard-code explanation text.
- The backend function and access gate shown in a popover come from
  `routes.generated.json`. CI regenerates it (`export_routes.py --check`) and
  vitest checks the registry against it, so a popover cannot name an endpoint the
  engine does not have. Those checks prove the endpoint exists; the wording of
  each explanation is reviewed by hand against the actual call.
- Controls marked **No backend call** run only in the browser. **No direct engine
  call** means the control itself sends nothing but leads to a request (for
  example, saving a key, after which the dashboard calls `GET /admin/identity`).

## Show wiring

The sidebar's **Show wiring** toggle adds a compact `METHOD /path` chip beside
every "How this works" hint, one per distinct wired endpoint in that hint. Hints
for controls that make no backend call get no chip. The chips come from the
same registry entries as the popovers. The toggle is off by default, and the
choice is kept in this browser's storage (`ie.dashboard.showWiring`). If storage
is unavailable, the choice lasts only for the page.

The toggle is in the sidebar brand block because the dashboard has no app-wide
top bar.

## Feature-switch states

Model actions that a kill switch disables (see
[Security: kill switches](security.md#kill-switches)) are shown disabled with the
switch named. For example:

> Model management is disabled (allow_model_management=false).

The state comes from `GET /admin/system` and is read-only. The dashboard never
changes configuration.

- **Explanation only.** Disabled controls stay visible and are explained. Each
  disabled button's `aria-describedby` points at the explanation: a shared notice
  for row actions, and a reason under the Add model form that covers every switch
  the selected mode needs.
- **Which actions each switch gates.** This matches the backend:
  - Import, load, unload and delete need `allow_model_management`.
  - Download also needs `allow_network_downloads`.
  - Listing models, model details and cancelling a download are not gated.
- **One fetch per operator session.** The switch state is fetched once per
  session and discarded when the key changes, is forgotten, or access is lost
  (see [Losing access mid-session](#losing-access-mid-session)). A late answer
  from an earlier session is ignored.
- **The `app.system` subsystem chain.** The "How this works" chain for this call
  lists the subsystems it reads, not a literal forwarding path. The handler reads
  the configured switches and routing summary (configuration, no subsystem call)
  and the readiness checks: a read-only SQLite probe and migration flag (Store)
  and backend availability (Backend pool). It also reads the scheduler's drain
  flag (Scheduler).
- **Refreshes.** The state refreshes from the Models **Refresh** button, when the
  browser comes back online, and after the engine refuses an action because of a
  switch.
- **Unknown state is shown as unknown.** If `/admin/system` cannot be read, the
  dashboard shows "Feature-switch status unavailable" with **Retry** and leaves
  actions enabled. The engine still refuses anything a switch disables. If a
  refresh fails after an earlier success, the last known state is kept and
  labelled as such.
- **Actions re-check before sending.** Actions check the switch again when they
  run, including after a confirmation dialog. The engine remains the authority.
- **Feature errors vs authorization errors.** Only the feature codes
  `model_management_disabled`, `downloads_disabled` and `diagnostics_disabled`,
  with status 403, are reported as a disabled switch. After such an error the
  dashboard treats that switch as off at once and refreshes. Any other 403,
  including `operator_role_required`, is reported as an authorization or server
  error, never as a disabled feature.

## Losing access mid-session

Some failures mean the saved key no longer grants operator access: a 401 (key
revoked or invalid, or authentication now required), or a 403 with
`operator_role_required` (a client key). If one of these comes back from any
protected request, the dashboard ends the session.

**Where it is detected.** Every protected request is covered:

- a view's data load;
- model-list polling;
- the live-metrics polling fallback (used when the metrics socket is unavailable
  or rejected);
- a switch-state refresh;
- a Models, Keys or Security action.

**What happens.**

- The failure is classified by the same rules as the sign-in gate, so the
  existing key prompt appears ("not accepted"), or the operator-access-required
  screen for a client key. No extra identity request is made, so an identity
  failure cannot trigger a loop.
- Everything tied to the session is discarded: its views, its switch state and
  any switches the engine refused during it. None of it is kept or shown as
  "stale".
- Each session has its own reporter. A failure that arrives late from an earlier
  session is ignored, so an old key's 401 cannot sign out a newly signed-in
  operator. A burst of failures ends a session only once.

**What does not end the session.**

- Feature 403s: these explain the switch and refresh the switch state.
- Other 403s, network errors and 5xx: these keep their normal error, Retry or
  last-known-state handling.
- Opening and cancelling **Change key**.

**Enforcement.** The engine enforces access on every request. This behaviour only
keeps the dashboard honest about the session.

## Documentation links

Registry `docs` references are repository paths with an optional heading
fragment (for example `docs/security.md#operator-access`). The docs are not
served by the engine, so they resolve to the canonical GitHub copy:

`https://github.com/dlroqa/inference-engine/blob/main/<path>`

Limitation: `main` is a moving branch, so the linked page can describe a newer
engine than the one running. There is no version-aware docs base yet. Links open
in a new tab so the dashboard keeps its state, and they carry only the static
registry path: never keys, model paths, request content or runtime state.

`tests/test_dashboard_wiring_docs.py` checks that every referenced file exists
and that every fragment matches a heading as GitHub slugifies it.

## Coverage and known limitations

`dashboard/src/__tests__/wiringCoverage.test.tsx` renders each view, gate and
confirmation dialog in populated, error and empty states. It requires every
control to have a registry scope explained by a visible hint on the same surface
(inside a dialog, only a hint in that dialog counts).

- Excluded by design: the hint buttons themselves and links inside an open
  popover (no help for help).
- Client rows on the Clients view are selected by clicking the row or with the
  client's own button (Enter or Space). The audit counts a clickable row as
  pointer-only unless it contains that button, and requires none to be.

## Live metrics states

The Overview's stat cards and Scheduler card read the live metrics stream
(`WS /ws/metrics`), or the polling fallback (`GET /metrics`). What they may claim
depends on the data, not only on the connection:

| Data | Connection | Shown |
| --- | --- | --- |
| None yet | connecting, live or polling | Loading; no numbers (no invented zeros) |
| None yet | disconnected | Metrics unavailable, reconnecting |
| Kept from before | disconnected (fallback failed) | The last data, marked as not current |
| Kept from before | fallback or reconnect started, no new data yet | The last data, marked as awaiting new data |
| New since the last disconnect | live (stream) or polling (REST fallback) | Current data |

**Live** is shown only after the stream delivers data. After a disconnect, the
badge reads **Polling** while the REST fallback is tried and while it delivers,
and **Disconnected** if it fails. A reconnecting stream takes over at its first
frame. From then on, an older poll that finishes late cannot replace newer data
or mark the stream down.

A scheduler that is not running is shown as such. A zero-length queue means
queueing is off. An average wait of null means no request has waited yet.
Energy is shown as measured only when the engine reports `state: measured`;
otherwise the card gives the engine's reason. A source name alone, such as
`rapl` while a baseline is being established, is not a measurement.

## Model details

**Details** on a Models row opens a drawer (`#/models/<id>`) that reads
`GET /admin/models/{model_id}`. It shows the following:

- status and compatibility;
- metadata;
- the SHA-256 checksum, with a copy button;
- the model's source;
- the last error.

**Presentation rules.**

- **Local imports:** the engine's `source_ref` for a local import is the file's
  path. The drawer never shows it and says "Local import (path not shown)" instead.
- **Remote sources:** a Hugging Face or URL source is shown as plain text, not as
  a link. A URL download made with this version records no address, so it shows
  "URL: address not stored". For older records, the engine removes credentials
  before sending the source: URL userinfo, and query values whose names look
  credential-bearing.
- **Error text:** it is shown as text. The engine applies the same credential
  redaction to every URL and query parameter in it, in list and detail
  responses alike.

**Limits.**

- This is not a promise that no text can ever contain a filesystem path. A model
  name, or an error the engine reported, may still name one.
- New download failures are stored and logged only as fixed messages (see
  [Security: log redaction](security.md#log-redaction)). Records from earlier
  versions may still hold raw error text in the database; responses redact it.

**Refresh.** The drawer refreshes from the model list's polling, not from a
separate loop:

- A changed row refreshes the details.
- A row that is gone after a successful list refresh triggers a refetch.
- **Not found** is shown only when the details request returns the engine's
  `404 model_not_found`. That can happen on any load, including the first one
  from a direct link. Any other 404 is an ordinary error, with Retry.
- A failed refresh keeps the last details, marked as not current, with Retry.

**Navigation and focus.**

- **Close, Escape or Back** close the drawer. Escape first closes an open
  "How this works" popover.
- **Opened from the list:** closing returns to that list entry and focuses the
  model's **Details** button. If the row is gone, focus goes to the Models heading.
- **Opened from a direct link:** closing replaces the address with `#/models`
  and focuses the heading.
- **Back and Forward** close and reopen the drawer.
- **Leaving the view** with the drawer open focuses the next page, as any
  navigation does.

## UI → subsystem → endpoint table

The table below is generated from the registry and the route inventory by
`dashboard/src/lib/wiringDocs.ts`. The ordinary vitest run fails if it is out of
date, but it never rewrites it.

**Regenerating.** Regeneration runs in GitHub Actions, never by hand-editing the
block:

1. The CI step **Wiring docs (generated diff)** runs `npm run docs:wiring`
   (`UPDATE_WIRING_DOCS=1`) and prints the diff.
2. It uploads the complete generated file as the `wiring-docs` artifact.
3. It then restores the tracked file and fails on drift.

The step is `scripts/ci_wiring_docs.sh`. Its restore runs from an EXIT trap.
Each failure is reported by name: generation, capturing the document, the diff,
or the restore. A failed restore fails the step even when nothing else failed.
`scripts/ci_wiring_docs_harness.sh` runs in the same job and tests these failure
paths against throwaway repositories.

To update, copy the generated block into this page and commit it.

**Contents.** Rows are sorted by control ID. Access is the route inventory's gate.
Kill switches are the configuration switches that must be on for the control to
work (read-only in the dashboard).

<!-- wiring-table:start -->
_Generated from `dashboard/src/lib/wiring.ts` and `routes.generated.json`; do not edit by hand._

**Controls that call the engine**

| Control | ID | Endpoint | Passes through | Access | Audited | Kill switches | Docs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Signed-in identity | `app.identity` | `GET /admin/identity` | Gateway → Keystore | `operator` | no | — | [Security: operator access](security.md#operator-access) |
| Feature-switch status | `app.system` | `GET /admin/system` | Gateway → Store → Backend pool → Scheduler | `operator` | no | — | [Security: kill switches](security.md#kill-switches) |
| Recent deliveries | `clients.deliveries` | `GET /admin/billing/webhooks/deliveries` | Webhooks | `operator` | no | — | — |
| Webhook endpoints | `clients.endpoints` | `GET /admin/billing/webhooks/endpoints` | Webhooks | `operator` | no | — | [Webhooks](webhooks.md) |
| Clients | `clients.list` | `GET /admin/billing/clients` | Billing | `operator` | no | — | [Billing](billing.md) |
| Create key | `keys.create` | `POST /admin/keys` | Keystore → Audit log | `operator` | yes | — | — |
| Delete revoked key | `keys.delete` | `DELETE /admin/keys/{key_id}` | Keystore → Audit log | `operator` | yes | — | — |
| API keys | `keys.list` | `GET /admin/keys` | Keystore | `operator` | no | — | — |
| Revoke key | `keys.revoke` | `DELETE /admin/keys/{key_id}` | Keystore → Gateway → Audit log | `operator` | yes | — | — |
| Logs | `logs.list` | `GET /logs` | Log buffer | `operator` | no | — | [Operability](operability.md) |
| Cancel download | `models.cancel` | `POST /admin/models/{model_id}/cancel` | Model service | `operator` | no | — | — |
| Delete model | `models.delete` | `DELETE /admin/models/{model_id}` | Model service → Model registry → Audit log | `operator` | yes | Model management (`allow_model_management`) | — |
| Model details | `models.detail` | `GET /admin/models/{model_id}` | Model registry → Model service | `operator` | no | — | [README: model lifecycle](../README.md#model-lifecycle-block-6) |
| Download model | `models.download` | `POST /admin/models/download` | Model service → Model registry → Audit log | `operator` | yes | Model management (`allow_model_management`); Network model downloads (`allow_network_downloads`) | — |
| Import model | `models.import` | `POST /admin/models/import` | Model service → Model registry → Audit log | `operator` | yes | Model management (`allow_model_management`) | — |
| Models | `models.list` | `GET /admin/models` | Model registry → Model service | `operator` | no | — | [README: model lifecycle](../README.md#model-lifecycle-block-6) |
| Load model | `models.load` | `POST /admin/models/{model_id}/load` | Model service → Backend pool → Backend → Audit log | `operator` | yes | Model management (`allow_model_management`) | — |
| Unload model | `models.unload` | `POST /admin/models/{model_id}/unload` | Model service → Backend → Audit log | `operator` | yes | Model management (`allow_model_management`) | — |
| Alerts | `monitoring.alerts` | `GET /admin/alerts` | Billing → Webhooks → Backend pool | `operator` | no | — | [Monitoring](monitoring.md) |
| Key attribution | `monitoring.attribution` | `GET /admin/usage/attribution` | Quota & usage → Keystore → Billing | `operator` | no | — | — |
| Error taxonomy | `monitoring.taxonomy` | `GET /admin/errors/taxonomy` | Log buffer | `operator` | no | — | — |
| Inference live feed | `overview.feed` | `WS /ws/feed` | Telemetry | `operator` | no | — | — |
| Live metrics | `overview.metrics` | `WS /ws/metrics` | Telemetry → Scheduler → Backend | `operator` | no | — | [Operability](operability.md) |
| Metrics snapshot (polling fallback) | `overview.metrics-fallback` | `GET /metrics` | Telemetry | `operator` | no | — | — |
| Engine version and readiness | `overview.readiness` | `GET /admin/overview` | Store → Backend | `operator` | no | — | — |
| Audit log and integrity check | `security.audit` | `GET /admin/audit` | Audit log | `operator` | no | — | [Security: tamper-evident audit log](security.md#tamper-evident-audit-log) |

**Controls with no backend call** (they change only what this browser shows or stores)

| Control | ID | What it does | Afterwards |
| --- | --- | --- | --- |
| Model source tabs | `local.add-source` | Switches which fields the form shows. Nothing is sent until you submit. | — |
| Close detail | `local.close-detail` | Closes this panel and updates the page address. | — |
| Close details | `local.close-drawer` | Closes the model details and returns the page address to #/models (back to the list entry you came from, when you opened them from the list). | — |
| Copy checksum | `local.copy-checksum` | Copies the model's SHA-256 checksum text to your clipboard. Nothing is sent to the engine. | — |
| Copy token | `local.copy-token` | Copies the new token to your clipboard. It is not sent anywhere and cannot be shown again. | — |
| Cancel | `local.dialog-cancel` | Closes this confirmation. Nothing is sent and nothing changes. | — |
| Text filter | `local.logs-filter` | Filters the log events already loaded in this browser. The server is not queried again. | — |
| Navigation | `local.navigation` | Changes the page address (#/view) in this browser. | The view that opens then loads its own data from the engine. |
| Saved operator key | `local.saved-key` | Stores the key in this browser's local storage, or removes it. | The dashboard then calls GET /admin/identity, sending the saved key (if any) as a Bearer token, to re-check who it is signed in as. |
| Select a client | `local.select-client` | Opens or closes the client's detail panel and updates the page address (#/clients/<id>). Click a row, or use the client's button with Enter or Space; keyboard focus stays on that button. | The panel then loads the client's webhook endpoints and recent deliveries. |
| Show wiring | `local.show-wiring` | Shows or hides the endpoint (METHOD /path) next to every "How this works" button. The choice is kept in this browser only; it is off by default. | — |
<!-- wiring-table:end -->
