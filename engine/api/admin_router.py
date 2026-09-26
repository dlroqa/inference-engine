"""Admin API for the operator dashboard (Block 5).

A small, typed control surface the dashboard consumes:

- ``GET  /admin/overview``      — health + readiness + model + current metrics.
- ``POST /admin/model/load``    — load the configured model (safe/idempotent).
- ``POST /admin/model/unload``  — unload the current model (safe/idempotent).
- ``GET  /admin/keys``          — list API keys (no secrets).
- ``POST /admin/keys``          — create a key (token returned **once**).
- ``DELETE /admin/keys/{id}``   — revoke a key.
- ``GET  /admin/identity``      — who the current operator caller is (never a token).
- ``GET  /admin/system``        — version, readiness, feature switches (booleans),
  and a routing-config summary — redacted, read-only.

Every endpoint is behind :func:`require_operator` (an operator key, or keyless
loopback development use while effective auth is off). Model control acts on the
single configured GGUF model (Block 1); a full model catalog is a later block.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from engine.api.deps import operator_identity, require_operator
from engine.api.errors import OpenAIError
from engine.api.health import readiness_state
from engine.auth.keys import KeyStore
from engine.buildinfo import build_info
from engine.inference.base import InferenceBackend
from engine.inference.factory import build_backend
from engine.inference.types import BackendError, BackendState
from engine.logging_setup import get_logger
from engine.store.db import connect_readonly
from engine.store.migrations import migrations_at_head
from engine.telemetry.service import Telemetry

router = APIRouter(prefix="/admin", tags=["admin"])
_log = get_logger("engine.admin")

_LOADED = (BackendState.READY, BackendState.GENERATING)


def _model_panel(request: Request, backend: InferenceBackend | None) -> dict[str, Any]:
    settings = request.app.state.settings
    loaded = backend is not None and backend.state in _LOADED
    return {
        "configured_id": settings.model_id,
        "configured": settings.model_path is not None,
        "state": backend.state.value if backend is not None else BackendState.UNLOADED.value,
        "loaded": loaded,
        "model_id": backend.capabilities().model_id if loaded and backend is not None else None,
    }


@router.get("/overview")
def overview(request: Request) -> dict[str, Any]:
    require_operator(request)
    settings = request.app.state.settings
    telemetry: Telemetry = request.app.state.telemetry
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)

    checks: dict[str, str] = {}
    ready = True
    try:
        conn = connect_readonly(settings.db_path)
        try:
            conn.execute("SELECT 1;").fetchone()
            checks["database"] = "ok"
            checks["migrations"] = "applied" if migrations_at_head(conn) else "pending"
            ready = checks["migrations"] == "applied"
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - db unreachable is rare in tests
        checks["database"] = f"error: {exc.__class__.__name__}"
        ready = False

    return {
        "version": request.app.version,
        "ready": ready,
        "checks": checks,
        "model": _model_panel(request, backend),
        "metrics": telemetry.build_snapshot(backend),
    }


@router.post("/model/load")
async def model_load(request: Request) -> dict[str, Any]:
    require_operator(request)
    settings = request.app.state.settings
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)

    if backend is not None and backend.state in _LOADED:
        return {"result": "already_loaded", "model": _model_panel(request, backend)}

    if settings.model_path is None:
        raise OpenAIError(
            "no model is configured; set model_path before loading",
            status_code=400,
            type="invalid_request_error",
            code="model_not_configured",
        )

    if backend is None:
        backend = build_backend(settings)
        request.app.state.backend = backend
    try:
        await backend.load()
    except BackendError as exc:
        _log.warning("model_load_failed", extra={"detail": str(exc)})
        raise OpenAIError(
            f"model failed to load: {exc}",
            status_code=503,
            type="service_unavailable",
            code="model_load_failed",
        ) from exc
    _log.info("model_loaded", extra={"model": settings.model_id})
    return {"result": "loaded", "model": _model_panel(request, backend)}


@router.post("/model/unload")
async def model_unload(request: Request) -> dict[str, Any]:
    require_operator(request)
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    if backend is None or backend.state not in _LOADED:
        return {"result": "already_unloaded", "model": _model_panel(request, backend)}
    await backend.unload()
    _log.info("model_unloaded")
    return {"result": "unloaded", "model": _model_panel(request, backend)}


@router.get("/backends")
def backends(request: Request) -> dict[str, Any]:
    """Per-backend routing status (Block 10, sub-slice 3): local + remote pool.

    Secret-free: names, kind, local/remote, state, availability, and in-flight —
    never a base URL credential. The registry decides placement per request.
    """
    require_operator(request)
    registry = request.app.state.backend_registry
    entries = registry.status()
    return {
        "backends": entries,
        "ready": registry.any_ready(),
        "count": len(entries),
    }


class RoutePlanRequest(BaseModel):
    model_config = {"extra": "forbid"}
    model: str
    prompt: str | None = None
    #: Request features to enforce during the dry-run (Block 12.1), e.g.
    #: ``["structured_output"]``; candidates are narrowed to supporting backends.
    required_features: list[str] = Field(default_factory=list)


@router.post("/route/plan")
def route_plan(request: Request, body: RoutePlanRequest) -> dict[str, Any]:
    """Dry-run (Block 10, sub-slice 5a): explain how a model name would route now.

    Resolves the route/cascade policy against current backend health/load and
    returns the chosen backend plus the candidates considered per step — without
    reserving a slot or generating anything.
    """
    require_operator(request)
    router_ = request.app.state.router
    settings = request.app.state.settings
    prefix_key = None
    if body.prompt and settings.prefix_affinity_chars > 0:
        prefix_key = body.prompt[: settings.prefix_affinity_chars]
    return router_.plan(body.model, prefix_key, frozenset(body.required_features))


@router.get("/routes")
def routes(request: Request) -> dict[str, Any]:
    """Per-route cost + performance (Block 10.5b + 12.2a), secret-free.

    Rows are keyed by (model, backend) with request/error/cancel counts, tokens,
    token cost, success rate, average total/first-token latency, and — added in
    Block 12.2a — average queue wait, average end-to-end output rate (tok/s),
    total upstream attempts, the backend tier, and count maps of the route
    ``reasons``/``fallbacks``/``policies`` seen; plus per-model admission sheds and
    overall totals. Measurement only — it does not change routing, and it never
    exposes prompts, completions, credentials, or headers (see docs/backends.md).
    """
    require_operator(request)
    return request.app.state.route_metrics.snapshot()


class KeyCreate(BaseModel):
    model_config = {"extra": "forbid"}
    label: str | None = Field(default=None, max_length=200)


@router.get("/keys")
def list_keys(request: Request) -> dict[str, Any]:
    require_operator(request)
    store: KeyStore = request.app.state.gateway.keys
    return {
        "keys": [
            {
                "id": r.id,
                "prefix": r.prefix,
                "label": r.label,
                "created_at": r.created_at,
                "last_used_at": r.last_used_at,
                "revoked": r.revoked,
                "role": r.role,
                "client_id": r.client_id,
            }
            for r in store.list()
        ]
    }


@router.post("/keys")
def create_key(request: Request, body: KeyCreate) -> dict[str, Any]:
    require_operator(request)
    store: KeyStore = request.app.state.gateway.keys
    record, token = store.create(label=body.label)
    _log.info("key_created", extra={"key_id": record.id})
    request.app.state.audit.record(
        "key.create",
        actor=operator_identity(request),
        target=record.id,
        detail={"label": body.label},
    )
    # The token is returned exactly once; it is never stored or shown again.
    return {
        "id": record.id,
        "prefix": record.prefix,
        "label": record.label,
        "created_at": record.created_at,
        "token": token,
    }


@router.delete("/keys/{key_id}")
def revoke_or_delete_key(
    request: Request,
    key_id: str,
    purge: bool = Query(default=False, description="Permanently delete an already-revoked key."),
) -> dict[str, Any]:
    require_operator(request)
    store: KeyStore = request.app.state.gateway.keys

    if purge:
        # Permanent removal is only allowed for already-revoked keys, so an active
        # key is never deleted out from under callers without first being revoked.
        record = store.get(key_id)
        if record is None:
            raise OpenAIError(
                f"key {key_id!r} not found",
                status_code=404,
                type="invalid_request_error",
                code="key_not_found",
            )
        if not record.revoked:
            raise OpenAIError(
                "revoke the key before deleting it",
                status_code=409,
                type="invalid_request_error",
                code="key_not_revoked",
            )
        store.delete(key_id)
        _log.info("key_deleted", extra={"key_id": key_id})
        request.app.state.audit.record(
            "key.delete", actor=operator_identity(request), target=key_id
        )
        return {"deleted": True, "id": key_id}

    revoked = store.revoke(key_id)
    if not revoked:
        raise OpenAIError(
            f"key {key_id!r} not found or already revoked",
            status_code=404,
            type="invalid_request_error",
            code="key_not_found",
        )
    _log.info("key_revoked", extra={"key_id": key_id})
    request.app.state.audit.record("key.revoke", actor=operator_identity(request), target=key_id)
    return {"revoked": True, "id": key_id}


@router.get("/audit")
def audit_log(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    verify: bool = Query(
        default=False, description="Recompute and report the hash-chain integrity."
    ),
) -> dict[str, Any]:
    """The tamper-evident operator/security audit log (Block 9b).

    Records only structured metadata (never prompts, responses, or secrets). With
    ``verify=true`` the hash chain is recomputed and its integrity reported.
    """
    require_operator(request)
    audit = request.app.state.audit
    events = [
        {
            "id": e.id,
            "ts": e.ts,
            "actor": e.actor,
            "action": e.action,
            "target": e.target,
            "detail": e.detail,
            "hash": e.hash,
        }
        for e in audit.list(limit=limit)
    ]
    body: dict[str, Any] = {"events": events}
    if verify:
        result = audit.verify()
        body["verify"] = {
            "ok": result.ok,
            "count": result.count,
            "first_bad_id": result.first_bad_id,
        }
    return body


@router.get("/identity")
def identity(request: Request) -> dict[str, Any]:
    """Who is calling: the presented operator key's safe metadata, or local dev.

    Never returns the token. With keyless loopback development access (effective
    auth off) the identity is reported explicitly as ``local``.
    """
    decision = require_operator(request)
    settings = request.app.state.settings
    key = decision.key
    return {
        "kind": "key" if key is not None else "local",
        "auth_required": settings.effective_require_auth(),
        "key": (
            {"id": key.id, "prefix": key.prefix, "label": key.label, "role": key.role}
            if key is not None
            else None
        ),
    }


def system_summary(request: Request) -> dict[str, Any]:
    """The redacted, read-only system description used by ``/admin/system``.

    Feature switches are booleans only; the gRPC port and billing provider are
    separate typed metadata. No URLs, credentials, secrets, or prompts appear.
    """
    settings = request.app.state.settings
    ready, checks, inference = readiness_state(request)
    scheduler = getattr(request.app.state, "scheduler", None)
    return {
        "build": build_info(),
        "readiness": {"ready": ready, "checks": checks, "inference": inference},
        "draining": bool(scheduler is not None and scheduler.is_draining),
        "switches": {
            "allow_model_management": bool(settings.allow_model_management),
            "allow_network_downloads": bool(settings.allow_network_downloads),
            "allow_structured_output": bool(settings.allow_structured_output),
            "diagnostics_enabled": bool(settings.diagnostics_enabled),
            "require_auth": bool(settings.effective_require_auth()),
            "webhooks_enabled": bool(settings.webhooks_enabled),
            "client_events_enabled": bool(settings.client_events_enabled),
            "ip_allowlist_set": bool(settings.ip_allowlist),
            "grpc_enabled": bool(settings.grpc_enabled),
        },
        "metadata": {
            "grpc_port": settings.grpc_port if settings.grpc_enabled else None,
            "billing_provider": settings.billing_provider or None,
        },
        "routing": {
            "backend_kind": settings.backend_kind,
            "virtual_models": [
                {"name": vm.name, "policy": vm.policy} for vm in settings.virtual_models
            ],
            "workload_routing_enabled": bool(settings.workload_routing_enabled),
            "workload_rule_count": len(settings.workload_routing_rules),
            "remote_workers": [w.name for w in settings.remote_workers],
            "spillover_providers": [p.name for p in settings.external_providers],
        },
    }


@router.get("/system")
def system(request: Request) -> dict[str, Any]:
    """Read-only system state for the operator UI (configuration is not editable)."""
    require_operator(request)
    return system_summary(request)
