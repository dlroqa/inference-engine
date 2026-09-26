# Operator dashboard: wiring explanations

The dashboard explains what each control does and which engine call it makes
through "How this works" popovers (`dashboard/src/components/WiredTo.tsx`).
This page describes how that content is sourced and checked. A generated
**UI → subsystem → endpoint** table is planned for this page in a later slice.

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
- Client rows on the Clients view are pointer-only (not keyboard-focusable). They
  are explained by a "Select a client" hint in the column header; making them
  keyboard-operable is a follow-up.
