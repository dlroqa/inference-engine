"""Billing store: plans, clients, subscriptions, entitlement resolution, and the
webhook idempotency ledger (Block 11.1)."""

from __future__ import annotations

from pathlib import Path

from engine.auth.keys import KeyStore
from engine.billing.store import BillingStore
from engine.quota.store import UsageStore
from engine.store.db import connect
from engine.store.migrations import apply_migrations


def _stores(tmp_path: Path, *, default_plan: str | None = None) -> tuple[BillingStore, KeyStore]:
    db = tmp_path / "billing.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return BillingStore(db, default_plan_id=default_plan), KeyStore(db)


def test_plan_roundtrip_with_allowed_models(tmp_path: Path) -> None:
    billing, _ = _stores(tmp_path)
    billing.upsert_plan(
        id="pro",
        name="Pro",
        quota_5h_cu=10.0,
        quota_weekly_cu=100.0,
        rate_limit_per_min=30,
        allowed_models=["m1", "m2"],
    )
    plan = billing.get_plan("pro")
    assert plan is not None
    assert plan.allowed_models == ("m1", "m2")
    assert plan.rate_limit_per_min == 30
    # Upsert updates in place.
    billing.upsert_plan(id="pro", name="Pro+", quota_5h_cu=20.0, quota_weekly_cu=200.0)
    plan = billing.get_plan("pro")
    assert plan is not None and plan.name == "Pro+" and plan.allowed_models is None


def test_unowned_key_has_no_entitlement(tmp_path: Path) -> None:
    billing, keys = _stores(tmp_path)
    record, _ = keys.create()
    access = billing.key_access(record.id)
    assert access.status == "active"
    assert access.entitlement is None  # falls back to engine-wide limits


def test_owned_key_resolves_active_subscription_plan(tmp_path: Path) -> None:
    billing, keys = _stores(tmp_path)
    billing.upsert_plan(id="pro", name="Pro", quota_5h_cu=10.0, quota_weekly_cu=100.0)
    client = billing.create_client(id="c1", external_ref="cus_1")
    record, _ = keys.create()
    billing.attach_key(record.id, client.id)
    billing.upsert_subscription(
        id="s1",
        client_id=client.id,
        plan_id="pro",
        provider="stripe",
        provider_sub_id="sub_1",
        status="active",
    )
    access = billing.key_access(record.id)
    assert access.status == "active"
    assert access.entitlement is not None
    assert access.entitlement.plan_id == "pro"
    assert access.entitlement.quota_5h_cu == 10.0


def test_default_plan_used_without_subscription(tmp_path: Path) -> None:
    billing, keys = _stores(tmp_path, default_plan="free")
    billing.upsert_plan(id="free", name="Free", quota_5h_cu=5.0, quota_weekly_cu=50.0)
    client = billing.create_client(id="c1")
    record, _ = keys.create()
    billing.attach_key(record.id, client.id)
    access = billing.key_access(record.id)
    assert access.entitlement is not None and access.entitlement.plan_id == "free"


def test_suspended_client_suspends_its_keys(tmp_path: Path) -> None:
    billing, keys = _stores(tmp_path)
    client = billing.create_client(id="c1")
    record, _ = keys.create()
    billing.attach_key(record.id, client.id)
    billing.set_client_status(client.id, "suspended")
    assert billing.key_access(record.id).status == "suspended"


def test_revoke_client_keys_is_terminal(tmp_path: Path) -> None:
    billing, keys = _stores(tmp_path)
    client = billing.create_client(id="c1")
    record, token = keys.create()
    billing.attach_key(record.id, client.id)
    assert billing.revoke_client_keys(client.id) == 1
    assert billing.key_access(record.id).status == "revoked"
    # revoked_at is set too, so the Block 3 verify path also rejects the token.
    assert keys.verify(token) is None


def test_webhook_idempotency_ledger(tmp_path: Path) -> None:
    billing, _ = _stores(tmp_path)
    assert billing.record_event_once("stripe", "evt_1", "x") is True
    assert billing.record_event_once("stripe", "evt_1", "x") is False  # duplicate
    billing.mark_event_processed("stripe", "evt_1", "activated")
    # forget lets a redelivery be reprocessed (used on transient failure).
    billing.forget_event("stripe", "evt_1")
    assert billing.record_event_once("stripe", "evt_1", "x") is True


def test_client_usage_reconciles_with_usage_events(tmp_path: Path) -> None:
    db = tmp_path / "billing.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    billing = BillingStore(db)
    keys = KeyStore(db)
    usage = UsageStore(db)

    client = billing.create_client(id="c1")
    k1, _ = keys.create()
    k2, _ = keys.create()
    billing.attach_key(k1.id, client.id)
    billing.attach_key(k2.id, client.id)
    now = 1_000_000.0
    usage.record(
        key_id=k1.id,
        request_id="r1",
        endpoint="/v1/chat/completions",
        model="m",
        prompt_tokens=10,
        completion_tokens=5,
        cu=15.0,
        status=200,
        ts=now - 10,
    )
    usage.record(
        key_id=k2.id,
        request_id="r2",
        endpoint="/v1/chat/completions",
        model="m",
        prompt_tokens=10,
        completion_tokens=5,
        cu=25.0,
        status=200,
        ts=now - 20,
    )
    # Sum across the client's keys equals the sum of the individual key windows.
    total = billing.client_usage_cu(client.id, now - 100)
    per_key = usage._sum_cu(k1.id, now - 100) + usage._sum_cu(k2.id, now - 100)
    assert total == per_key == 40.0
