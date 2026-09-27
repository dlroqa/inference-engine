"""Shutdown keeps both store locks until the whole cleanup has run and every
store writer has stopped, even when the shutdown is cancelled (repeatedly).

The webhook delivery worker is the non-download store writer used here. Its
batch (``DeliveryWorker.run_once``) is replaced at the production seam by one
that blocks at a barrier, then makes a real write to the store's database. There
is no active download, so the download guard (``owns_work``) cannot mask a
premature release. Every wait is bounded, every gate is released in ``finally``,
and competitor checks take real OS locks: the same database with another model
directory, and the same model directory with another database.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.billing.webhooks import DeliveryWorker
from engine.config import Settings
from engine.inference.scheduler import Scheduler
from engine.main import create_app
from engine.store.db import connect
from engine.store.ownership import StoreLockedError, StoreOwnership
from engine.telemetry.service import Telemetry

WAIT = 30  # seconds; upper bound for every blocking wait


class Harness:
    """Gates and an ordered trace for one engine lifespan."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.settings = Settings(
            data_dir=tmp_path / "data",
            webhooks_enabled=True,
            webhook_poll_interval_s=0.1,
        )
        self.trace: list[str] = []
        self.batch_started = threading.Event()
        self.release_batch = threading.Event()
        self.stop_requested = threading.Event()
        self.drain_entered = threading.Event()
        self.release_drain = threading.Event()
        self.telemetry_entered = threading.Event()
        self.release_telemetry = threading.Event()
        self.gate_drain = False
        self.gate_telemetry = False
        self.fail_drain = False

    def release_all(self) -> None:
        for gate in (self.release_batch, self.release_drain, self.release_telemetry):
            gate.set()

    # -- competitors (real OS locks) --------------------------------------------

    def _probe(self, db: Path, models: Path) -> bool:
        probe = StoreOwnership(db, models)
        try:
            probe.acquire()
        except StoreLockedError:
            return False
        probe.release()
        return True

    def database_free(self) -> bool:
        """The same database, with a model directory nobody else uses."""
        return self._probe(Path(self.settings.db_path), self.tmp_path / "other-models")

    def model_directory_free(self) -> bool:
        """The same model directory, with a database nobody else uses."""
        return self._probe(self.tmp_path / "other.db", Path(self.settings.models_dir))

    def competing_engine_refused(self) -> bool:
        other = Settings(
            data_dir=self.tmp_path / "competitor",
            db_path=self.settings.db_path,
            models_dir=self.tmp_path / "competitor-models",
        )
        try:
            with TestClient(create_app(other)):
                return False
        except StoreLockedError:
            return True

    def final_writes(self) -> int:
        conn = sqlite3.connect(str(self.settings.db_path))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM shutdown_probe").fetchone()[0])
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    h = Harness(tmp_path)
    db_path = h.settings.db_path

    def run_once(self: DeliveryWorker) -> int:
        # The first batch blocks until released, then writes to the store; this
        # is the writer that must finish before the store is released.
        if h.batch_started.is_set():
            return 0
        h.trace.append("batch_started")
        h.batch_started.set()
        h.release_batch.wait(WAIT)
        if getattr(self, "_stop", None) is not None and self._stop.is_set():
            h.trace.append("stop_seen_by_batch")
        conn = connect(db_path)  # type: ignore[arg-type]
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS shutdown_probe (note TEXT)")
                conn.execute("INSERT INTO shutdown_probe VALUES ('final')")
        finally:
            conn.close()
        h.trace.append("final_write")
        return 0

    real_request_stop = getattr(DeliveryWorker, "request_stop", None)

    def request_stop(self: DeliveryWorker) -> None:
        h.trace.append("stop_requested")
        h.stop_requested.set()
        if real_request_stop is not None:
            real_request_stop(self)

    real_wait_drained = Scheduler.wait_drained

    async def wait_drained(self: Scheduler, timeout_s: float) -> bool:
        h.trace.append("drain_entered")
        h.drain_entered.set()
        if h.gate_drain:
            await asyncio.to_thread(h.release_drain.wait, WAIT)
        if h.fail_drain:
            raise RuntimeError("drain failed")
        return await real_wait_drained(self, timeout_s)

    real_telemetry_stop = Telemetry.stop

    async def telemetry_stop(self: Telemetry) -> None:
        h.trace.append("telemetry_stop")
        h.telemetry_entered.set()
        if h.gate_telemetry:
            await asyncio.to_thread(h.release_telemetry.wait, WAIT)
        await real_telemetry_stop(self)

    real_release = StoreOwnership.release

    def release(self: StoreOwnership) -> None:
        h.trace.append("store_released")
        real_release(self)

    monkeypatch.setattr(DeliveryWorker, "run_once", run_once)
    monkeypatch.setattr(DeliveryWorker, "request_stop", request_stop, raising=False)
    monkeypatch.setattr(Scheduler, "wait_drained", wait_drained)
    monkeypatch.setattr(Telemetry, "stop", telemetry_stop)
    monkeypatch.setattr(StoreOwnership, "release", release)
    try:
        yield h
    finally:
        h.release_all()


async def _wait(event: threading.Event) -> bool:
    return await asyncio.to_thread(event.wait, WAIT)


async def _still_running(task: asyncio.Task[Any]) -> bool:
    """Bounded check that ``task`` has not finished (a negative observation)."""
    done, _ = await asyncio.wait({task}, timeout=0.3)
    return not done


def _start(h: Harness) -> tuple[Any, asyncio.Event, asyncio.Task[None]]:
    app = create_app(h.settings)
    stop = asyncio.Event()

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            await stop.wait()

    return app, stop, asyncio.create_task(serve())


async def _assert_owned(h: Harness, when: str) -> None:
    assert not await asyncio.to_thread(h.database_free), f"{when}: the database was released"
    assert not await asyncio.to_thread(h.model_directory_free), (
        f"{when}: the model directory was released"
    )


def test_cancelled_shutdown_waits_for_the_webhook_writer_before_release(harness: Harness) -> None:
    h = harness
    h.gate_drain = True

    async def main() -> None:
        app, stop, lifespan = _start(h)
        try:
            assert await _wait(h.batch_started), "the webhook batch never started"
            assert app.state.model_service.owns_work() is False  # no download guard
            stop.set()  # begin shutdown
            assert await _wait(h.drain_entered), "shutdown never reached the drain step"

            # 1. Cancel while blocked before webhook cleanup; the writer is live.
            lifespan.cancel()
            assert await _still_running(lifespan), "shutdown finished while a writer was live"
            await _assert_owned(h, "cancelled before webhook cleanup")
            assert await asyncio.to_thread(h.competing_engine_refused)
            assert h.final_writes() == 0

            # 2. Let cleanup reach the webhook worker; it is asked to stop and
            #    its batch is held just before its final write. Cancel again.
            h.release_drain.set()
            assert await _wait(h.stop_requested), "cleanup never asked the webhook worker to stop"
            lifespan.cancel()
            assert await _still_running(lifespan), "shutdown finished during webhook finalization"
            await _assert_owned(h, "cancelled during webhook finalization")
        finally:
            h.release_all()

        # 3. The final write happens, then the rest of cleanup, then release.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(lifespan), WAIT)

    asyncio.run(main())
    assert h.final_writes() == 1
    order = h.trace
    for later in ("telemetry_stop", "store_released"):
        assert order.index("final_write") < order.index(later), order
    assert order.index("telemetry_stop") < order.index("store_released"), order
    assert order.count("store_released") == 1
    assert h.database_free() and h.model_directory_free()


def test_normal_shutdown_runs_cleanup_once_and_releases_both(harness: Harness) -> None:
    h = harness
    h.release_batch.set()

    async def main() -> None:
        _, stop, lifespan = _start(h)
        assert await _wait(h.batch_started)
        stop.set()
        await asyncio.wait_for(lifespan, WAIT)

    asyncio.run(main())
    assert h.trace.count("telemetry_stop") == 1
    assert h.trace.count("store_released") == 1
    assert h.database_free() and h.model_directory_free()


def test_cancellation_during_a_later_step_still_finishes_cleanup(harness: Harness) -> None:
    h = harness
    h.release_batch.set()
    h.gate_telemetry = True

    async def main() -> None:
        _, stop, lifespan = _start(h)
        try:
            assert await _wait(h.batch_started)
            stop.set()
            assert await _wait(h.telemetry_entered)
            lifespan.cancel()
            assert await _still_running(lifespan)
            await _assert_owned(h, "cancelled during telemetry stop")
        finally:
            h.release_all()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(lifespan), WAIT)

    asyncio.run(main())
    assert h.trace.index("telemetry_stop") < h.trace.index("store_released")
    assert h.database_free() and h.model_directory_free()


def test_an_ordinary_failure_before_webhook_cleanup_still_stops_the_writer(
    harness: Harness,
) -> None:
    h = harness
    h.fail_drain = True
    h.release_batch.set()

    async def main() -> None:
        _, stop, lifespan = _start(h)
        assert await _wait(h.batch_started)
        stop.set()
        with pytest.raises(RuntimeError, match="drain failed"):
            await asyncio.wait_for(lifespan, WAIT)

    asyncio.run(main())
    assert h.final_writes() == 1
    assert h.trace.index("final_write") < h.trace.index("store_released")
    assert "telemetry_stop" in h.trace  # later steps still ran
    assert h.database_free() and h.model_directory_free()


def test_cancellation_takes_precedence_over_a_cleanup_failure(harness: Harness) -> None:
    h = harness
    h.fail_drain = True
    events: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append(record.getMessage())

    # create_app resets the root logger's handlers, so listen on the engine's logger.
    startup_logger = logging.getLogger("engine.startup")
    handler = _Capture(level=logging.WARNING)
    startup_logger.addHandler(handler)

    async def main() -> None:
        _, stop, lifespan = _start(h)
        try:
            assert await _wait(h.batch_started)
            stop.set()
            assert await _wait(h.stop_requested)
            lifespan.cancel()
            assert await _still_running(lifespan)
            await _assert_owned(h, "cancelled after a failed step")
        finally:
            h.release_all()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(lifespan), WAIT)

    try:
        asyncio.run(main())
    finally:
        startup_logger.removeHandler(handler)
    assert h.final_writes() == 1
    assert "shutdown_failed_while_cancelled" in events
    assert h.database_free() and h.model_directory_free()


def test_a_startup_failure_after_the_writer_started_stops_it_first(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import engine.grpc.server as grpc_server_module

    h = harness
    h.release_batch.set()
    h.settings.grpc_enabled = True  # the last, awaited, startup step

    async def failing_grpc(*args: Any, **kwargs: Any) -> Any:
        # Runs after the webhook worker started; let its batch begin first.
        assert await _wait(h.batch_started)
        raise RuntimeError("startup failed late")

    monkeypatch.setattr(grpc_server_module, "create_grpc_server", failing_grpc)

    async def main() -> None:
        _, _, lifespan = _start(h)
        with pytest.raises(RuntimeError, match="startup failed late"):
            await asyncio.wait_for(lifespan, WAIT)

    asyncio.run(main())
    assert h.final_writes() == 1
    assert h.trace.index("final_write") < h.trace.index("store_released")
    assert h.database_free() and h.model_directory_free()


def test_ownership_is_retained_while_a_writer_outlives_cleanup(harness: Harness) -> None:
    """Fail closed: if cleanup itself is cut short while the writer runs, the
    store stays owned (and says so) instead of being released."""
    h = harness
    h.gate_drain = True

    async def main() -> Any:
        app, stop, lifespan = _start(h)
        try:
            assert await _wait(h.batch_started)
            stop.set()
            assert await _wait(h.drain_entered)
            # Cut the cleanup task itself short (as a loop teardown would) before
            # it reaches the webhook worker, which is still writing.
            [cleanup] = [
                t
                for t in asyncio.all_tasks()
                if getattr(t.get_coro(), "__qualname__", "").endswith("teardown")
            ]
            cleanup.cancel()
            await asyncio.wait_for(asyncio.shield(lifespan), WAIT)
            # Shutdown returned, but the writer is still running: still owned.
            assert h.final_writes() == 0
            await _assert_owned(h, "cleanup cut short with a live writer")
            assert getattr(app.state, "store_ownership_retained", None) is not None
        finally:
            h.release_all()
        return app

    app = asyncio.run(main())
    assert h.final_writes() == 1
    assert "store_released" not in h.trace
    app.state.store_ownership_retained.release()  # test cleanup
    assert h.database_free() and h.model_directory_free()
