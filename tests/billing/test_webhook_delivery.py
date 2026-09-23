"""Delivery worker: success, retry/backoff, dead-letter, signing headers."""

from __future__ import annotations

from pathlib import Path

from engine.billing.store import BillingStore
from engine.billing.webhooks import signing
from engine.billing.webhooks.delivery import DeliveryWorker, TransportError
from engine.billing.webhooks.store import WebhookStore
from engine.store.db import connect
from engine.store.migrations import apply_migrations


class FakeTransport:
    """Records POSTs and returns a scripted status code (or raises)."""

    def __init__(self, status: int | None = 200, raise_error: bool = False) -> None:
        self.status = status
        self.raise_error = raise_error
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, body: str, headers: dict[str, str], timeout: float) -> int:
        self.calls.append({"url": url, "body": body, "headers": headers})
        if self.raise_error:
            raise TransportError("connection refused")
        assert self.status is not None
        return self.status


def _setup(tmp_path: Path) -> tuple[WebhookStore, str, str]:
    db = tmp_path / "wh.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    billing = BillingStore(db)
    billing.create_client(id="c1")
    store = WebhookStore(db)
    endpoint, secret = store.create_endpoint(client_id="c1", url="https://hooks.example.com/x")
    store.enqueue_event(
        client_id="c1",
        event_id="evt_1",
        event_type="subscription.activated",
        payload='{"id":"evt_1"}',
        now=1_000.0,
    )
    return store, endpoint.id, secret


def test_success_marks_succeeded_and_signs(tmp_path: Path) -> None:
    store, endpoint_id, secret = _setup(tmp_path)
    transport = FakeTransport(status=200)
    worker = DeliveryWorker(store, transport=transport, clock=lambda: 1_000.0)
    assert worker.run_once() == 1
    d = store.list_deliveries()[0]
    assert d.status == "succeeded" and d.attempts == 1 and d.last_status_code == 200
    # The delivery carried a valid Standard Webhooks signature.
    call = transport.calls[0]
    headers = call["headers"]
    assert signing.verify(
        secret,
        headers["webhook-signature"],
        headers["webhook-id"],  # type: ignore[index]
        int(headers["webhook-timestamp"]),
        call["body"],  # type: ignore[index,arg-type]
    )


def test_failure_schedules_retry_with_backoff(tmp_path: Path) -> None:
    store, _, _ = _setup(tmp_path)
    transport = FakeTransport(status=500)
    worker = DeliveryWorker(
        store,
        transport=transport,
        backoff_schedule_s=(30.0, 120.0),
        max_attempts=5,
        clock=lambda: 1_000.0,
    )
    worker.run_once()
    d = store.list_deliveries()[0]
    assert d.status == "pending" and d.attempts == 1
    assert d.next_attempt_at == 1_000.0 + 30.0  # first backoff delay
    assert not store.claim_due(now=1_000.0)  # not due until backoff elapses
    assert store.claim_due(now=1_030.0)  # due after the delay


def test_dead_letter_after_max_attempts(tmp_path: Path) -> None:
    store, _, _ = _setup(tmp_path)
    transport = FakeTransport(raise_error=True)  # always a network error
    clock = {"now": 1_000.0}
    worker = DeliveryWorker(
        store,
        transport=transport,
        backoff_schedule_s=(1.0,),
        max_attempts=3,
        clock=lambda: clock["now"],
    )
    # Attempt 1 and 2 -> retry (each backs off 1s); attempt 3 -> dead.
    for now in (1_000.0, 1_001.0, 1_002.0):
        clock["now"] = now
        worker.run_once()
    d = store.list_deliveries()[0]
    assert d.status == "dead" and d.attempts == 3
    assert d.last_error is not None


def test_disabled_endpoint_dead_letters_without_send(tmp_path: Path) -> None:
    store, endpoint_id, _ = _setup(tmp_path)
    store.set_disabled(endpoint_id, True)
    transport = FakeTransport(status=200)
    worker = DeliveryWorker(store, transport=transport, clock=lambda: 1_000.0)
    worker.run_once()
    d = store.list_deliveries()[0]
    assert d.status == "dead"
    assert transport.calls == []  # nothing was sent
