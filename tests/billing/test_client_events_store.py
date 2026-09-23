"""ClientEventLog, notifier, and emitter unit behavior (Block 11.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from engine.billing.client_events import (
    ClientEventEmitter,
    ClientEventLog,
    ClientEventNotifier,
)
from engine.billing.store import BillingStore
from engine.store.db import connect
from engine.store.migrations import apply_migrations


def _log(tmp_path: Path, **kwargs: object) -> ClientEventLog:
    db = tmp_path / "ce.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    BillingStore(db).create_client(id="c1")
    BillingStore(db).create_client(id="c2")
    return ClientEventLog(db, **kwargs)  # type: ignore[arg-type]


def test_append_is_ordered_and_idempotent(tmp_path: Path) -> None:
    log = _log(tmp_path)
    i1 = log.append(client_id="c1", event_id="e1", event_type="x", payload="{}")
    i2 = log.append(client_id="c1", event_id="e2", event_type="x", payload="{}")
    assert i1 is not None and i2 is not None and i2 > i1
    # Same (client, event_id) -> no new row.
    assert log.append(client_id="c1", event_id="e1", event_type="x", payload="{}") is None


def test_list_since_and_isolation(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append(client_id="c1", event_id="a", event_type="x", payload="{}")
    log.append(client_id="c2", event_id="b", event_type="x", payload="{}")
    log.append(client_id="c1", event_id="c", event_type="x", payload="{}")
    c1 = log.list_since("c1", 0)
    assert [e.event_id for e in c1] == ["a", "c"]  # only c1's events, ordered
    # Cursor resume returns only newer events.
    assert [e.event_id for e in log.list_since("c1", c1[0].id)] == ["c"]


def test_retention_max_per_client(tmp_path: Path) -> None:
    log = _log(tmp_path, retention_max_per_client=2)
    for i in range(4):
        log.append(client_id="c1", event_id=f"e{i}", event_type="x", payload="{}")
    kept = [e.event_id for e in log.list_since("c1", 0)]
    assert kept == ["e2", "e3"]  # only the two newest survive


def test_retention_max_age(tmp_path: Path) -> None:
    log = _log(tmp_path, retention_max_age_s=100.0)
    log.append(client_id="c1", event_id="old", event_type="x", payload="{}", now=1_000.0)
    # A later append triggers pruning of rows older than now-100.
    log.append(client_id="c1", event_id="new", event_type="x", payload="{}", now=1_200.0)
    assert [e.event_id for e in log.list_since("c1", 0)] == ["new"]


def test_notifier_wakes_waiter() -> None:
    async def run() -> None:
        notifier = ClientEventNotifier()
        waiter = notifier.event("c1")
        assert not waiter.is_set()
        notifier.notify("c1")
        await asyncio.wait_for(waiter.wait(), timeout=1.0)
        # After notify, a fresh event is unset again.
        assert not notifier.event("c1").is_set()

    asyncio.run(run())


def test_emitter_writes_log_and_notifies(tmp_path: Path) -> None:
    log = _log(tmp_path)

    class FakeDispatcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def emit(self, *, client_id, event_type, data, event_id=None, now=None) -> int:  # type: ignore[no-untyped-def]
            self.calls.append(event_type)
            return 1

    notifier = ClientEventNotifier()
    notified: list[str] = []
    orig = notifier.notify
    notifier.notify = lambda cid: (notified.append(cid), orig(cid))[1]  # type: ignore[assignment,method-assign]
    dispatcher = FakeDispatcher()
    emitter = ClientEventEmitter(dispatcher=dispatcher, event_log=log, notifier=notifier)

    emitter.emit(client_id="c1", event_type="subscription.activated", data={"k": 1}, event_id="e1")
    # Fanned out to both channels.
    assert dispatcher.calls == ["subscription.activated"]
    assert [e.event_id for e in log.list_since("c1", 0)] == ["e1"]
    assert notified == ["c1"]

    # Idempotent re-emit: dispatcher still called, but no new log row / notify.
    emitter.emit(client_id="c1", event_type="subscription.activated", data={"k": 1}, event_id="e1")
    assert len(log.list_since("c1", 0)) == 1
    assert notified == ["c1"]
