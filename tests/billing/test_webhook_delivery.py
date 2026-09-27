"""Delivery worker: success, retry/backoff, dead-letter, signing headers."""

from __future__ import annotations

import asyncio
import threading
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


class GatedTransport(FakeTransport):
    """Blocks the first POST until released, so a stop can arrive mid-delivery."""

    def __init__(self) -> None:
        super().__init__(status=200)
        self.entered = threading.Event()
        self.release = threading.Event()

    def post(self, url: str, body: str, headers: dict[str, str], timeout: float) -> int:
        status = super().post(url, body, headers, timeout)
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(30), "the first delivery was never released"
        return status


def test_stop_finishes_the_current_delivery_and_leaves_the_rest_pending(
    tmp_path: Path,
) -> None:
    """Through the real batch loop: a stop during the first delivery lets it
    finish and be recorded; the other due deliveries are not sent and stay
    pending, untouched, for a later run."""
    store, _, _ = _setup(tmp_path)
    for n in (2, 3):
        store.enqueue_event(
            client_id="c1",
            event_id=f"evt_{n}",
            event_type="subscription.activated",
            payload=f'{{"id":"evt_{n}"}}',
            now=1_000.0 + n,
        )
    transport = GatedTransport()
    worker = DeliveryWorker(store, transport=transport, clock=lambda: 2_000.0)

    async def main() -> None:
        loop_task = asyncio.create_task(worker.run_loop(poll_interval_s=0.05))
        try:
            assert await asyncio.to_thread(transport.entered.wait, 30)
            worker.request_stop()
            loop_task.cancel()  # as shutdown does: the task ends only after the batch
            done, _ = await asyncio.wait({loop_task}, timeout=0.3)
            assert not done, "the loop ended while its batch was still delivering"
        finally:
            transport.release.set()
        try:
            await asyncio.wait_for(asyncio.shield(loop_task), 30)
        except asyncio.CancelledError:
            pass
        assert worker.idle

    asyncio.run(main())
    assert len(transport.calls) == 1, "a delivery after the stop was sent"
    by_event = {d.event_id: d for d in store.list_deliveries()}
    first = by_event["evt_1"]
    assert first.status == "succeeded" and first.attempts == 1
    for event_id in ("evt_2", "evt_3"):
        rest = by_event[event_id]
        assert rest.status == "pending" and rest.attempts == 0, rest
        assert rest.last_status_code is None and rest.last_error is None
