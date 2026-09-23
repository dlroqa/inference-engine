"""Stripe signature verification and lifecycle mapping (Block 11.1)."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import pytest

from engine.auth.keys import KeyStore
from engine.billing import stripe as stripe_adapter
from engine.billing.store import BillingStore
from engine.store.db import connect
from engine.store.migrations import apply_migrations

SECRET = "whsec_test"


def _sign(payload: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _billing(tmp_path: Path) -> tuple[BillingStore, KeyStore]:
    db = tmp_path / "b.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return BillingStore(db), KeyStore(db)


# ---- signature verification ------------------------------------------------


def test_valid_signature_accepts() -> None:
    payload = b'{"id":"evt_1"}'
    stripe_adapter.verify_signature(payload, _sign(payload), SECRET)  # no raise


def test_invalid_signature_rejected() -> None:
    payload = b'{"id":"evt_1"}'
    header = _sign(payload, secret="wrong-secret")
    with pytest.raises(stripe_adapter.SignatureError):
        stripe_adapter.verify_signature(payload, header, SECRET)


def test_tampered_payload_rejected() -> None:
    header = _sign(b'{"id":"evt_1"}')
    with pytest.raises(stripe_adapter.SignatureError):
        stripe_adapter.verify_signature(b'{"id":"evt_TAMPERED"}', header, SECRET)


def test_stale_timestamp_rejected() -> None:
    payload = b'{"id":"evt_1"}'
    header = _sign(payload, ts=int(time.time()) - 10_000)
    with pytest.raises(stripe_adapter.SignatureError):
        stripe_adapter.verify_signature(payload, header, SECRET, tolerance_s=300)


def test_missing_or_malformed_header_rejected() -> None:
    with pytest.raises(stripe_adapter.SignatureError):
        stripe_adapter.verify_signature(b"{}", None, SECRET)
    with pytest.raises(stripe_adapter.SignatureError):
        stripe_adapter.verify_signature(b"{}", "garbage", SECRET)


# ---- lifecycle mapping -----------------------------------------------------


def _event(event_type: str, obj: dict) -> dict:
    return {"id": "evt_x", "type": event_type, "data": {"object": obj}}


def test_subscription_created_activates_and_sets_plan(tmp_path: Path) -> None:
    billing, _ = _billing(tmp_path)
    billing.upsert_plan(id="pro", name="Pro", quota_5h_cu=10.0, quota_weekly_cu=100.0)
    result = stripe_adapter.handle_event(
        billing,
        _event(
            "customer.subscription.created",
            {"id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"plan": "pro"}},
        ),
    )
    assert result.action == "activated"
    client = billing.get_client_by_external_ref("cus_1")
    assert client is not None and client.status == "active"
    sub = billing.get_subscription_by_provider("stripe", "sub_1")
    assert sub is not None and sub.plan_id == "pro" and sub.status == "active"


def test_unknown_plan_falls_back_and_never_widens(tmp_path: Path) -> None:
    billing, _ = _billing(tmp_path)
    # No such plan "enterprise" exists; the handler must not invent one.
    stripe_adapter.handle_event(
        billing,
        _event(
            "customer.subscription.created",
            {
                "id": "sub_1",
                "customer": "cus_1",
                "status": "active",
                "metadata": {"plan": "enterprise"},
            },
        ),
    )
    sub = billing.get_subscription_by_provider("stripe", "sub_1")
    assert sub is not None and sub.plan_id is None  # falls back, no entitlement conjured


def test_payment_failed_suspends(tmp_path: Path) -> None:
    billing, _ = _billing(tmp_path)
    billing.create_client(id="c1", external_ref="cus_1")
    result = stripe_adapter.handle_event(
        billing, _event("invoice.payment_failed", {"customer": "cus_1"})
    )
    assert result.action == "suspended"
    assert billing.get_client("c1").status == "suspended"  # type: ignore[union-attr]


def test_subscription_deleted_revokes_keys(tmp_path: Path) -> None:
    billing, keys = _billing(tmp_path)
    client = billing.create_client(id="c1", external_ref="cus_1")
    record, token = keys.create()
    billing.attach_key(record.id, client.id)
    result = stripe_adapter.handle_event(
        billing,
        _event("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"}),
    )
    assert result.action == "canceled"
    assert billing.get_client("c1").status == "canceled"  # type: ignore[union-attr]
    assert billing.key_access(record.id).status == "revoked"
    assert keys.verify(token) is None


def test_unknown_event_type_ignored(tmp_path: Path) -> None:
    billing, _ = _billing(tmp_path)
    result = stripe_adapter.handle_event(billing, _event("charge.refunded", {"id": "ch_1"}))
    assert result.action == "ignored"
