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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from engine.api.deps import require_operator
from engine.api.errors import error_response
from engine.billing import stripe as stripe_adapter
from engine.billing.store import BillingStore
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
    _log.info(
        "stripe_webhook_processed",
        extra={"event_id": event_id, "event_type": event_type, "action": result.action},
    )
    return JSONResponse({"status": "processed", "action": result.action})


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
