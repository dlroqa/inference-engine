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
