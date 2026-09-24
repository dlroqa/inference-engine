# Block 12.1 — Heterogeneous model eligibility

## Purpose

Block 10 provides a registry of local, vLLM, SGLang, and optional external
backends, but assumes that every entry exposes one client-facing `model_id`.
`build_remote_worker()` currently assigns `"local-model"` to every remote
worker, and a physical-model request is placed across the whole pool.

This slice makes physical-model placement deterministic and model-aware: a
request for model `X` is placed only on a ready backend whose live
`capabilities().model_id` is `X`. It retains the existing homogeneous default,
does not introduce benchmark-driven routing, and is fully testable with the
existing fake backends.

This is the foundation for the later Block 12 slices. It does **not** add
multi-model adapters, adaptive routing, engine chaining, or post-stream
failover.

## Invariants

- A physical model request can never be placed on an engine serving a different
  model.
- A request requiring a feature (currently structured output) can never be
  placed on an eligible engine that lacks that feature.
- An unknown model returns `404`; a configured, known model with no ready
  eligible backend returns `503`; an eligible set that is ready but full returns
  the existing retriable saturation response (`429` OpenAI, `529` Anthropic).
- Existing configurations without worker `model_id` values remain homogeneous:
  all workers expose `"local-model"`.
- Client model names are unique. A virtual-model name must not shadow a physical
  model name.

## Model sources of truth

Each `BackendEntry` has two distinct model values:

- **Live model:** `backend.capabilities().model_id`, read only when
  `entry.is_ready()` is true. This is the placement source of truth and tracks
  primary admin load/unload swaps.
- **Configured fallback:** `served_model`, populated at application assembly.
  It keeps a model known while its backend is unloaded or failed its health
  probe, so the API returns `503` rather than incorrectly claiming `404`.

Do not use `is_loaded()` for the live-model branch. A remote backend with a
failed health probe is not ready and must use its configured fallback instead.

## Configuration and construction

### `engine/config.py`

Extend `RemoteWorkerSpec`:

```python
model_id: str | None = None
```

Document it as the client-facing name requested by clients; `None` means the
shared `"local-model"` default. `model` remains the upstream/provider model
name. `extra="forbid"` continues to reject misspellings.

`RemoteWorkerSpec` is also the schema for `external_providers`, so this applies
to external entries without a second configuration type.

At settings/application validation, reject duplicate backend names and reject a
`virtual_models[].name` that collides with any configured physical model name:
the primary `settings.model_id`, a worker's effective model ID, or an external
provider's effective model ID. Dynamic primary model changes must also have a
defined result: reject a load that would collide, or reserve virtual names and
reject such a load. Do not silently let virtual routing shadow a physical model.

### `engine/inference/factory.py`

In `build_remote_worker()`, set `RemoteBackendConfig.model_id` to:

```python
spec.model_id or "local-model"
```

This value is client-facing only; preserve `spec.model` as `remote_model` sent
to the upstream server.

### `engine/main.py`

Populate `BackendEntry.served_model`:

- primary: `settings.model_id`;
- remote worker and external provider: `spec.model_id or "local-model"`.

Do not pass raw `spec.model_id`, since `None` would lose the backward-compatible
fallback identity.

## Registry and router

### `engine/inference/registry.py`

Add `served_model: str | None = None` to `BackendEntry` and:

```python
def current_model(self) -> str | None:
    if self.is_ready():
        backend = self.backend()
        return backend.capabilities().model_id if backend is not None else None
    return self.served_model
```

Add:

- `known_models() -> set[str]`: non-`None` `current_model()` values across the
  registry.
- `representative_for(model: str) -> InferenceBackend | None`: the first ready
  backend with matching live model ID. This is only a diagnostic/convenience
  helper; it is not permission to validate a request against a different backend
  from the one that will be leased.

Extend `select()` and `acquire()` with a physical `model: str | None` filter.
Apply it before spillover tiers, prefix affinity, and least-busy selection. A
model-filtered request must calculate `NoBackendAvailable.reason` over the same
model-eligible subset: `busy` if an eligible backend is ready, otherwise
`unloaded`.

### Capability-safe placement

Model filtering alone is insufficient when engines serving the same model have
different capabilities. A representative backend can say structured output is
supported while the least-busy selected backend does not support it.

Add a request-feature eligibility mechanism to registry/router selection (for
example, a `required_features` argument or a capability predicate) and apply it
to **both** dry-run and lease acquisition. It must:

1. Narrow candidates by physical model and every requested feature before
   affinity, spillover, or least-busy logic.
2. Permit a structured-output request only to a backend whose *live*
   capabilities support it.
3. Distinguish “model has a ready eligible backend, but none support this
   feature” (`400 structured_output_unsupported`) from “no ready backend serves
   this known model” (`503`) and “supporting candidates are full” (saturation).
4. Make candidate output from `Router.plan()` reflect the exact constrained
   candidate set.

Do not solve this by checking one `representative_for()` result. Do not claim
structured-output support for a virtual route unless every possible selected
backend is feature-eligible, or feature-aware selection narrows the virtual
route before a lease is acquired.

### `engine/inference/router.py`

- `known_model(name)` is true when `name` is a virtual name or is in
  `registry.known_models()`.
- `model_names()` returns a stable, deduplicated list: sorted physical names plus
  virtual names in a documented stable order. Physical/virtual collisions must
  already have been rejected.
- For a physical model, `acquire()` and `plan()` pass `model=name` into registry
  selection and filter candidate rows using the same predicate.
- Virtual route/cascade scopes retain their configured backend scopes, but all
  request features must be applied consistently to every virtual candidate.
- Remove `_base_model_id()` and all homogeneous-pool assumptions.

## API and gRPC behavior

### OpenAI and Anthropic edges

Check `router.known_model(model_id)` **before** looking for a ready backend.
This preserves `404` for truly unknown models even when every backend is down.

For a physical model with no ready `representative_for(model_id)`, return the
normal `503` path; never fall back to `registry.representative()` for that
physical request. For a virtual model, use the feature-aware router/registry
preflight and acquisition path rather than an arbitrary representative.

Move capability validation to, or bind it to, the same feature-aware placement
decision that reserves the backend lease. This prevents a time-of-check/
time-of-placement mismatch. Keep all checks before response headers or the first
streaming event.

Update stale comments that describe the pool as homogeneous. `/v1/models`, the
gRPC `ListModels` method, and client service discovery already consume
`router.model_names()` and should expose the heterogeneous union after this
change.

### Readiness and status

Update `/readyz`: when `require_model_ready=true`, readiness must use
`backend_registry.any_ready()` rather than only `app.state.backend`. A remote
worker must keep the service ready when the primary is unloaded.

`GET /admin/backends` should show the live `model_id` for ready entries and,
where useful, the configured `served_model` for unavailable entries so operators
can understand why a model remains known but temporarily unavailable. Keep the
response secret-free.

## Documentation

- Update `docs/backends.md` with a “Heterogeneous pools & model eligibility
  (Block 12.1)” section, including the effective default and a two-worker
  example with distinct `model_id` values.
- Correct homogeneous-pool statements and clarify that external spillover is
  used only after eligible non-spillover candidates cannot admit work; an
  ineligible local backend is not a local candidate.
- Update `docs/compatibility.md` and RFC §10 with the concrete per-route
  model/feature matrix. A feature is supported on a route only when placement
  enforces that fact.
- Update `config.example.toml` with `model_id` and preserve an explicit example
  of its default behavior.

## Tests

Add or extend tests for all of the following:

- `RemoteWorkerSpec.model_id` defaults to `None`, accepts an override, and
  `build_remote_worker()` maps it to the expected client-facing ID.
- Registry physical-model selection/acquisition never selects an ineligible
  entry; `busy` versus `unloaded` is calculated only over model-eligible entries.
- `known_models()` is the union of live ready IDs and configured unavailable
  fallbacks; `representative_for()` returns only a matching ready entry.
- Homogeneous default entries still expose and route `"local-model"` unchanged.
- Router physical acquisition and route plans list only eligible candidates;
  virtual models preserve scope while applying feature eligibility.
- A structured-output request selects only a supporting eligible backend, even
  when a non-supporting engine with the same model is less busy. Test all-full
  supporting candidates, no supporting candidate, and virtual route/cascade
  cases.
- API behavior: model served by one engine reaches that engine; a configured but
  down model returns `503`; a truly unknown model returns `404`; no unrelated
  backend determines a physical model's capability error.
- Model-list output is deduplicated and stable; physical/virtual name collisions
  fail deterministically.
- `/readyz` remains ready with a healthy worker and unloaded primary when
  `require_model_ready=true`.
- Existing homogeneous, virtual-model, gRPC, and external-spillover regression
  tests remain green.

## Verification

Run:

```bash
.venv/bin/ruff check .
.venv/bin/mypy engine
.venv/bin/python -m pytest -q
```

The current baseline contains 441 collected tests; do not retain the stale
“~361 existing tests” claim.

Keep `scripts/remote_smoke.py` as an adapter smoke test, but do not describe it
as heterogeneous-routing coverage: it creates one adapter directly and
hardcodes the client-facing `"smoke-model"` ID. Add an app-level smoke test or
new script that starts/configures two distinct mapped workers and verifies:

1. `GET /v1/models` lists both physical IDs;
2. a request for each ID reaches only its corresponding fake/remote worker;
3. `GET /admin/backends` reports each mapping; and
4. `POST /admin/route/plan` shows only the eligible candidates, including any
   requested feature constraint.

Real vLLM/SGLang validation remains an operator task on GPU hosts. CI should
exercise the same contracts with fake OpenAI-compatible servers.

## Explicitly deferred

- Multiple client-facing models per adapter/backend.
- Adaptive or benchmark-driven routing (12.2–12.3).
- Per-engine pool routers and topology evolution (12.4–12.5).
- Feature precision that is not enforced at selection time.
