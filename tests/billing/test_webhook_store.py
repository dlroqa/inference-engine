"""Webhook store: endpoints, secret rotation, fan-out idempotency, queue, replay."""

from __future__ import annotations

from pathlib import Path

from engine.billing.store import BillingStore
from engine.billing.webhooks.store import WebhookStore
from engine.store.db import connect
from engine.store.migrations import apply_migrations


def _stores(tmp_path: Path) -> tuple[WebhookStore, BillingStore]:
    db = tmp_path / "wh.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    billing = BillingStore(db)
    billing.create_client(id="c1")
    return WebhookStore(db), billing


def test_create_endpoint_returns_secret_once(tmp_path: Path) -> None:
    store, _ = _stores(tmp_path)
    endpoint, secret = store.create_endpoint(client_id="c1", url="https://h/x")
    assert secret.startswith("whsec_")
    assert store.active_secrets(endpoint.id) == [secret]


def test_enqueue_fans_out_and_is_idempotent(tmp_path: Path) -> None:
    store, _ = _stores(tmp_path)
    e1, _ = store.create_endpoint(client_id="c1", url="https://h/1")
    e2, _ = store.create_endpoint(client_id="c1", url="https://h/2")
    n = store.enqueue_event(
        client_id="c1", event_id="evt_1", event_type="subscription.activated", payload="{}"
    )
    assert n == 2  # one per endpoint
    # Re-enqueue same event -> no new rows (idempotent per endpoint+event).
    assert (
        store.enqueue_event(
            client_id="c1", event_id="evt_1", event_type="subscription.activated", payload="{}"
        )
        == 0
    )
    assert len(store.list_deliveries()) == 2
    assert {d.endpoint_id for d in store.list_deliveries()} == {e1.id, e2.id}


def test_enqueue_respects_event_type_filter(tmp_path: Path) -> None:
    store, _ = _stores(tmp_path)
    store.create_endpoint(client_id="c1", url="https://h/1", event_types=["subscription.canceled"])
    # Endpoint only wants canceled events; an activated event is not enqueued.
    assert (
        store.enqueue_event(
            client_id="c1", event_id="e1", event_type="subscription.activated", payload="{}"
        )
        == 0
    )
    assert (
        store.enqueue_event(
            client_id="c1", event_id="e2", event_type="subscription.canceled", payload="{}"
        )
        == 1
    )


def test_disabled_endpoint_gets_no_deliveries(tmp_path: Path) -> None:
    store, _ = _stores(tmp_path)
    ep, _ = store.create_endpoint(client_id="c1", url="https://h/1")
    store.set_disabled(ep.id, True)
    assert store.enqueue_event(client_id="c1", event_id="e1", event_type="x", payload="{}") == 0


def test_rotation_yields_two_active_secrets_then_expires(tmp_path: Path) -> None:
    store, _ = _stores(tmp_path)
    ep, first = store.create_endpoint(client_id="c1", url="https://h/1")
    now = 1_000.0
    second = store.rotate_secret(ep.id, grace_s=100.0, now=now)
    # During grace both are active.
    assert set(store.active_secrets(ep.id, now=now)) == {first, second}
    # After grace only the new one remains.
    assert store.active_secrets(ep.id, now=now + 200.0) == [second]


def test_claim_due_and_replay(tmp_path: Path) -> None:
    store, _ = _stores(tmp_path)
    store.create_endpoint(client_id="c1", url="https://h/1")
    store.enqueue_event(client_id="c1", event_id="e1", event_type="x", payload="{}", now=100.0)
    assert len(store.claim_due(now=100.0)) == 1
    d = store.list_deliveries()[0]
    store.mark_dead(d.id, status_code=500, error="boom", now=100.0)
    assert store.get_delivery(d.id).status == "dead"  # type: ignore[union-attr]
    assert not store.claim_due(now=100.0)  # dead is not due
    # Replay resets to pending, due now.
    assert store.replay(d.id, now=200.0) is True
    again = store.get_delivery(d.id)
    assert again.status == "pending" and again.attempts == 0  # type: ignore[union-attr]
    assert len(store.claim_due(now=200.0)) == 1
