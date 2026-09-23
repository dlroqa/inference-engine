"""Billing API (Block 11.1).

Two surfaces:

- ``POST /billing/webhooks/stripe`` — the public, provider-facing endpoint. It is
  **not** behind the operator gate; it authenticates the caller by verifying the
  Stripe signature over the raw body *before* parsing, records the event id for
  idempotency, and maps the event to a client lifecycle action.
- ``/admin/billing/*`` — operator controls (behind :func:`require_operator`) to
  define plans, register clients, attach keys, and read usage reconciliation.

Entitlement values live only in operator-defined plans; a webhook selects a plan
by id but can never define its limits.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from engine.api.deps import require_operator
from engine.api.errors import error_response
from engine.billing import stripe as stripe_adapter
from engine.billing.store import BillingStore
from engine.billing.webhooks.store import WebhookStore
from engine.config import _host_allowed
from engine.logging_setup import get_logger
from engine.quota.windows import rolling_5h_start, week_start

router = APIRouter(tags=["billing"])
_log = get_logger("engine.billing")


def _billing(request: Request) -> BillingStore | None:
    return getattr(request.app.state, "billing", None)


# --------------------------------------------------------------------------
# Public provider webhook
# --------------------------------------------------------------------------


@router.post("/billing/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    billing = _billing(request)
    enabled = (
        billing is not None
        and settings.billing_provider == "stripe"
        and settings.stripe_webhook_secret
    )
    if not enabled:
        return error_response(
            "stripe billing is not enabled",
            status_code=404,
            code="billing_disabled",
        )
    assert billing is not None  # narrowed by ``enabled`` above; for the type-checker

    raw = await request.body()
    # Verify the signature before parsing or acting on the payload.
    try:
        stripe_adapter.verify_signature(
            raw,
            request.headers.get("stripe-signature"),
            settings.stripe_webhook_secret,
            tolerance_s=settings.stripe_signature_tolerance_s,
        )
    except stripe_adapter.SignatureError as exc:
        _log.warning("stripe_webhook_signature_rejected", extra={"detail": str(exc)})
        return error_response(
            "invalid webhook signature",
            status_code=400,
            type="invalid_request_error",
            code="invalid_signature",
        )

    try:
        event: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response("malformed webhook body", status_code=400, code="invalid_payload")

    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        return error_response("webhook missing event id", status_code=400, code="invalid_payload")
    event_type = event.get("type")

    # Idempotency: a duplicate delivery is a no-op success.
    if not billing.record_event_once(stripe_adapter.PROVIDER, event_id, event_type):
        return JSONResponse({"status": "duplicate", "event_id": event_id})

    try:
        result = stripe_adapter.handle_event(billing, event)
    except Exception as exc:  # transient/internal — let Stripe retry.
        # Forget the event id so the redelivery is processed rather than dropped.
        billing.forget_event(stripe_adapter.PROVIDER, event_id)
        _log.error(
            "stripe_webhook_processing_failed",
            extra={"event_id": event_id, "event_type": event_type},
            exc_info=exc,
        )
        return error_response(
            "webhook processing failed",
            status_code=500,
            type="api_error",
            code="webhook_processing_error",
        )

    billing.mark_event_processed(stripe_adapter.PROVIDER, event_id, result.action)
    _emit_lifecycle(request, result, event_id)
    _log.info(
        "stripe_webhook_processed",
        extra={"event_id": event_id, "event_type": event_type, "action": result.action},
    )
    return JSONResponse({"status": "processed", "action": result.action})


# Map an inbound-lifecycle action to an outbound event type (Block 11.2).
_LIFECYCLE_EVENT_TYPES = {
    "activated": "subscription.activated",
    "plan_changed": "subscription.updated",
    "suspended": "subscription.suspended",
    "canceled": "subscription.canceled",
}


def _emit_lifecycle(request: Request, result: Any, source_event_id: str) -> None:
    """Emit an account event mirroring a billing lifecycle change, if any.

    The emitter fans out to both channels: outbound webhooks and the durable
    client event log (SSE). Each honors its own enabled flag.
    """
    emitter = getattr(request.app.state, "client_event_emitter", None)
    if emitter is None or result.client_id is None:
        return
    event_type = _LIFECYCLE_EVENT_TYPES.get(result.action)
    if event_type is None:
        return
    # Derive a deterministic event id from the source so a re-processed inbound
    # event never fans out a duplicate.
    emitter.emit(
        client_id=result.client_id,
        event_type=event_type,
        data={"client_id": result.client_id, "action": result.action},
        event_id=f"{source_event_id}.{result.action}",
    )


# --------------------------------------------------------------------------
# Operator controls
# --------------------------------------------------------------------------


class PlanUpsert(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    quota_5h_cu: float = Field(default=0.0, ge=0)
    quota_weekly_cu: float = Field(default=0.0, ge=0)
    rate_limit_per_min: int | None = Field(default=None, ge=0)
    allowed_models: list[str] | None = None


class ClientCreate(BaseModel):
    external_ref: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=320)


class KeyForClient(BaseModel):
    label: str | None = Field(default=None, max_length=256)


def _require_billing(request: Request) -> BillingStore:
    billing = _billing(request)
    # The store is always constructed in create_app; assert for the type-checker.
    assert billing is not None, "billing store not initialized"
    return billing


@router.post("/admin/billing/plans")
def upsert_plan(request: Request, body: PlanUpsert) -> dict[str, Any]:
    require_operator(request)
    billing = _require_billing(request)
    plan = billing.upsert_plan(
        id=body.id,
        name=body.name,
        quota_5h_cu=body.quota_5h_cu,
        quota_weekly_cu=body.quota_weekly_cu,
        rate_limit_per_min=body.rate_limit_per_min,
        allowed_models=body.allowed_models,
    )
    return _plan_dict(plan)


@router.get("/admin/billing/plans")
def list_plans(request: Request) -> dict[str, Any]:
    require_operator(request)
    billing = _require_billing(request)
    return {"plans": [_plan_dict(p) for p in billing.list_plans()]}


@router.post("/admin/billing/clients")
def create_client(request: Request, body: ClientCreate) -> dict[str, Any]:
    require_operator(request)
    billing = _require_billing(request)
    client = billing.create_client(
        id=uuid.uuid4().hex, external_ref=body.external_ref, email=body.email
    )
    return {
        "id": client.id,
        "external_ref": client.external_ref,
        "email": client.email,
        "status": client.status,
    }


@router.get("/admin/billing/clients")
def list_clients(request: Request) -> dict[str, Any]:
    require_operator(request)
    billing = _require_billing(request)
    return {
        "clients": [
            {
                "id": c.id,
                "external_ref": c.external_ref,
                "email": c.email,
                "status": c.status,
            }
            for c in billing.list_clients()
        ]
    }


@router.post("/admin/billing/clients/{client_id}/keys")
def create_key_for_client(request: Request, client_id: str, body: KeyForClient) -> dict[str, Any]:
    require_operator(request)
    billing = _require_billing(request)
    if billing.get_client(client_id) is None:
        return error_response(  # type: ignore[return-value]
            "unknown client", status_code=404, code="client_not_found"
        )
    key_store = request.app.state.gateway.keys
    record, token = key_store.create(label=body.label)
    billing.attach_key(record.id, client_id)
    # The token is displayed exactly once, at creation.
    return {
        "id": record.id,
        "prefix": record.prefix,
        "label": record.label,
        "client_id": client_id,
        "token": token,
    }


@router.get("/admin/billing/clients/{client_id}/reconcile")
def reconcile_client(request: Request, client_id: str) -> dict[str, Any]:
    """Reconcile a client's enforced usage against the attribution log.

    Both figures are summed from ``usage_events``, so they must agree; a mismatch
    would indicate corruption. Returns the per-window CU for the client's keys.
    """
    require_operator(request)
    billing = _require_billing(request)
    if billing.get_client(client_id) is None:
        return error_response(  # type: ignore[return-value]
            "unknown client", status_code=404, code="client_not_found"
        )
    now = time.time()
    return {
        "client_id": client_id,
        "cu_5h": billing.client_usage_cu(client_id, rolling_5h_start(now)),
        "cu_week": billing.client_usage_cu(client_id, week_start(now)),
    }


def _plan_dict(plan: Any) -> dict[str, Any]:
    return {
        "id": plan.id,
        "name": plan.name,
        "quota_5h_cu": plan.quota_5h_cu,
        "quota_weekly_cu": plan.quota_weekly_cu,
        "rate_limit_per_min": plan.rate_limit_per_min,
        "allowed_models": list(plan.allowed_models) if plan.allowed_models is not None else None,
    }


# --------------------------------------------------------------------------
# Outbound webhooks (Block 11.2) — operator controls
# --------------------------------------------------------------------------


class EndpointCreate(BaseModel):
    client_id: str = Field(min_length=1)
    url: str = Field(min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=512)
    event_types: list[str] | None = None


def _webhooks(request: Request) -> WebhookStore | None:
    return getattr(request.app.state, "webhook_store", None)


def _require_webhooks(request: Request) -> WebhookStore:
    store = _webhooks(request)
    assert store is not None, "webhook store not initialized"
    return store


def _endpoint_dict(ep: Any) -> dict[str, Any]:
    return {
        "id": ep.id,
        "client_id": ep.client_id,
        "url": ep.url,
        "description": ep.description,
        "disabled": ep.disabled,
        "event_types": list(ep.event_types) if ep.event_types is not None else None,
    }


def _delivery_dict(d: Any) -> dict[str, Any]:
    return {
        "id": d.id,
        "endpoint_id": d.endpoint_id,
        "event_id": d.event_id,
        "event_type": d.event_type,
        "status": d.status,
        "attempts": d.attempts,
        "next_attempt_at": d.next_attempt_at,
        "last_status_code": d.last_status_code,
        "last_error": d.last_error,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


@router.post("/admin/billing/webhooks/endpoints")
def create_endpoint(request: Request, body: EndpointCreate) -> dict[str, Any]:
    require_operator(request)
    billing = _require_billing(request)
    store = _require_webhooks(request)
    settings = request.app.state.settings
    if billing.get_client(body.client_id) is None:
        return error_response(  # type: ignore[return-value]
            "unknown client", status_code=404, code="client_not_found"
        )
    # Egress policy: the destination host must be on the allowlist, and the scheme
    # must be http(s). This is the same control external providers use.
    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return error_response(  # type: ignore[return-value]
            "endpoint url must be an absolute http(s) url",
            status_code=400,
            code="invalid_endpoint_url",
        )
    if not _host_allowed(parsed.hostname, settings.egress_allowlist):
        return error_response(  # type: ignore[return-value]
            "endpoint host is not in egress_allowlist",
            status_code=400,
            code="egress_not_allowed",
        )
    endpoint, secret = store.create_endpoint(
        client_id=body.client_id,
        url=body.url,
        description=body.description,
        event_types=body.event_types,
    )
    # The signing secret is displayed exactly once, at creation.
    return {**_endpoint_dict(endpoint), "secret": secret}


@router.get("/admin/billing/webhooks/endpoints")
def list_endpoints(request: Request, client_id: str | None = None) -> dict[str, Any]:
    require_operator(request)
    store = _require_webhooks(request)
    return {"endpoints": [_endpoint_dict(e) for e in store.list_endpoints(client_id)]}


@router.post("/admin/billing/webhooks/endpoints/{endpoint_id}/disable")
def disable_endpoint(request: Request, endpoint_id: str, disabled: bool = True) -> dict[str, Any]:
    require_operator(request)
    store = _require_webhooks(request)
    if not store.set_disabled(endpoint_id, disabled):
        return error_response(  # type: ignore[return-value]
            "unknown endpoint", status_code=404, code="endpoint_not_found"
        )
    return {"id": endpoint_id, "disabled": disabled}


@router.delete("/admin/billing/webhooks/endpoints/{endpoint_id}")
def delete_endpoint(request: Request, endpoint_id: str) -> dict[str, Any]:
    require_operator(request)
    store = _require_webhooks(request)
    if not store.delete_endpoint(endpoint_id):
        return error_response(  # type: ignore[return-value]
            "unknown endpoint", status_code=404, code="endpoint_not_found"
        )
    return {"id": endpoint_id, "deleted": True}


@router.post("/admin/billing/webhooks/endpoints/{endpoint_id}/rotate-secret")
def rotate_secret(request: Request, endpoint_id: str) -> dict[str, Any]:
    require_operator(request)
    store = _require_webhooks(request)
    settings = request.app.state.settings
    if store.get_endpoint(endpoint_id) is None:
        return error_response(  # type: ignore[return-value]
            "unknown endpoint", status_code=404, code="endpoint_not_found"
        )
    secret = store.rotate_secret(endpoint_id, grace_s=settings.webhook_signing_rotation_grace_s)
    return {"id": endpoint_id, "secret": secret}


@router.get("/admin/billing/webhooks/deliveries")
def list_deliveries(
    request: Request,
    endpoint_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    require_operator(request)
    store = _require_webhooks(request)
    deliveries = store.list_deliveries(endpoint_id=endpoint_id, status=status, limit=limit)
    return {"deliveries": [_delivery_dict(d) for d in deliveries]}


@router.post("/admin/billing/webhooks/deliveries/{delivery_id}/replay")
def replay_delivery(request: Request, delivery_id: str) -> dict[str, Any]:
    require_operator(request)
    store = _require_webhooks(request)
    if not store.replay(delivery_id):
        return error_response(  # type: ignore[return-value]
            "unknown delivery", status_code=404, code="delivery_not_found"
        )
    return {"id": delivery_id, "status": "pending"}
