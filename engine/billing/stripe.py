"""Stripe adapter: inbound webhook signature verification and lifecycle mapping.

Verification follows Stripe's documented scheme without depending on the Stripe
SDK: the ``Stripe-Signature`` header carries ``t=<unix>,v1=<hex hmac>``; the
signed payload is ``"{t}.{raw_body}"`` and the expected signature is
``HMAC-SHA256(signing_secret, signed_payload)``. We compare in constant time and
reject stale timestamps outside a tolerance window (replay protection).

Lifecycle mapping is deliberately small (Block 11.1):

- ``customer.subscription.created`` / ``updated`` (active/trialing) -> activate the
  client and set its plan entitlement.
- ``invoice.payment_failed`` -> suspend the client (reversible).
- ``customer.subscription.deleted`` -> cancel + terminally revoke the client's keys.

Plans are resolved by id from ``metadata.plan`` (an operator-controlled value set
on the Stripe object); an unknown plan falls back to the store's default plan.
The payload never defines entitlement *values*.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any

from engine.billing.store import BillingStore

PROVIDER = "stripe"
_ACTIVE_SUB_STATES = {"active", "trialing"}


class SignatureError(Exception):
    """Raised when a webhook signature is missing, malformed, stale, or invalid."""


def _parse_sig_header(header: str) -> tuple[int, list[str]]:
    """Return ``(timestamp, [v1 signatures])`` from a Stripe-Signature header."""
    timestamp = -1
    v1: list[str] = []
    for part in header.split(","):
        if "=" not in part:
            continue
        prefix, _, value = part.partition("=")
        prefix = prefix.strip()
        if prefix == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = -1
        elif prefix == "v1":
            v1.append(value.strip())
    return timestamp, v1


def verify_signature(
    payload: bytes,
    header: str | None,
    secret: str,
    *,
    tolerance_s: int = 300,
    now: float | None = None,
) -> None:
    """Verify a Stripe webhook signature, raising :class:`SignatureError` on failure.

    Verifies before any parsing/handling of the payload, so an invalid signature
    never changes state.
    """
    if not header:
        raise SignatureError("missing signature header")
    if not secret:
        raise SignatureError("no signing secret configured")

    timestamp, signatures = _parse_sig_header(header)
    if timestamp < 0 or not signatures:
        raise SignatureError("malformed signature header")

    now = time.time() if now is None else now
    if tolerance_s > 0 and abs(now - timestamp) > tolerance_s:
        raise SignatureError("timestamp outside tolerance")

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    # A single valid v1 signature is sufficient (Stripe may send several).
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("signature mismatch")


@dataclass(slots=True)
class LifecycleResult:
    action: str  # activated | plan_changed | suspended | canceled | ignored
    client_id: str | None = None


def _ensure_client(store: BillingStore, customer_ref: str, email: str | None) -> str:
    """Return the client id for a Stripe customer ref, creating it if needed."""
    client = store.get_client_by_external_ref(customer_ref)
    if client is not None:
        return client.id
    client = store.create_client(id=uuid.uuid4().hex, external_ref=customer_ref, email=email)
    return client.id


def _resolve_plan_id(store: BillingStore, obj: dict[str, Any]) -> str | None:
    """Resolve an operator-defined plan id from object metadata, if it exists."""
    metadata = obj.get("metadata") or {}
    candidate = metadata.get("plan")
    if candidate and store.get_plan(candidate) is not None:
        return candidate
    return None  # store falls back to its default plan


def handle_event(store: BillingStore, event: dict[str, Any]) -> LifecycleResult:
    """Apply one already-verified Stripe event to the billing store.

    Unknown event types are ignored (a no-op success), so Stripe's broad delivery
    set never triggers retries. Missing correlation data is also treated as an
    ignorable no-op rather than an error.
    """
    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        customer_ref = obj.get("customer")
        sub_id = obj.get("id")
        if not customer_ref or not sub_id:
            return LifecycleResult(action="ignored")
        client_id = _ensure_client(store, customer_ref, obj.get("email"))
        sub_status = obj.get("status", "")
        plan_id = _resolve_plan_id(store, obj)
        store.upsert_subscription(
            id=sub_id,
            client_id=client_id,
            plan_id=plan_id,
            provider=PROVIDER,
            provider_sub_id=sub_id,
            status="active" if sub_status in _ACTIVE_SUB_STATES else sub_status,
            current_period_end=_period_end(obj),
        )
        if sub_status in _ACTIVE_SUB_STATES:
            store.set_client_status(client_id, "active")
            return LifecycleResult(action="activated", client_id=client_id)
        return LifecycleResult(action="plan_changed", client_id=client_id)

    if event_type == "invoice.payment_failed":
        customer_ref = obj.get("customer")
        if not customer_ref:
            return LifecycleResult(action="ignored")
        client = store.get_client_by_external_ref(customer_ref)
        if client is None:
            return LifecycleResult(action="ignored")
        store.set_client_status(client.id, "suspended")
        return LifecycleResult(action="suspended", client_id=client.id)

    if event_type == "customer.subscription.deleted":
        customer_ref = obj.get("customer")
        sub_id = obj.get("id")
        if not customer_ref:
            return LifecycleResult(action="ignored")
        client = store.get_client_by_external_ref(customer_ref)
        if client is None:
            return LifecycleResult(action="ignored")
        if sub_id:
            existing = store.get_subscription_by_provider(PROVIDER, sub_id)
            store.upsert_subscription(
                id=sub_id,
                client_id=client.id,
                plan_id=existing.plan_id if existing is not None else None,
                provider=PROVIDER,
                provider_sub_id=sub_id,
                status="canceled",
                current_period_end=_period_end(obj),
            )
        store.set_client_status(client.id, "canceled")
        store.revoke_client_keys(client.id)
        return LifecycleResult(action="canceled", client_id=client.id)

    return LifecycleResult(action="ignored")


def _period_end(obj: dict[str, Any]) -> str | None:
    value = obj.get("current_period_end")
    return str(value) if value is not None else None
