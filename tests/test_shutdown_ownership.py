"""Shutdown keeps both store locks until the whole cleanup has run and every
store writer has stopped, even when the shutdown is cancelled (repeatedly).

The webhook delivery worker is the non-download store writer used here. Its
batch (``DeliveryWorker.run_once``) is replaced at the production seam by one
that blocks at a barrier, then makes a real write to the store's database. There
is no active download, so the download guard (``owns_work``) cannot mask a
premature release. Every wait is bounded, every gate is released in ``finally``,
and competitor checks take real OS locks: the same database with another model
directory, and the same model directory with another database.

The gRPC edge is replaced at its production seam (``create_grpc_server``) by a
double whose in-flight request is a real store writer: a thread that, once
released, records the request's final usage in the database. Its ``stop`` can
fail or block before termination is established. In those tests webhooks are
off and no download runs, so only the gRPC state can keep the store owned.
"""

from __future__ import annotations

import asyncio
import hashlib
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
        self.extra_releases: list[Any] = []

    def release_all(self) -> None:
        for gate in (self.release_batch, self.release_drain, self.release_telemetry):
            gate.set()
        for release in self.extra_releases:
            release()

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

    def write(self, note: str) -> None:
        conn = connect(self.settings.db_path)  # type: ignore[arg-type]
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS shutdown_probe (note TEXT)")
                conn.execute("INSERT INTO shutdown_probe VALUES (?)", (note,))
        finally:
            conn.close()

    def store_snapshot(self) -> tuple[str, ...]:
        """Contents of the protected database files and the model directory."""
        db = Path(self.settings.db_path)
        parts = []
        for path in (db, db.with_name(db.name + "-wal")):
            data = path.read_bytes() if path.exists() else b""
            parts.append(f"{path.name}:{hashlib.sha256(data).hexdigest()}")
        models = Path(self.settings.models_dir)
        parts.extend(sorted(p.name for p in models.iterdir()) if models.exists() else [])
        return tuple(parts)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    h = Harness(tmp_path)

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
        h.write("final")
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


class FakeGrpcEdge:
    """Stands in for the gRPC edge returned by ``create_grpc_server``.

    ``start`` begins one in-flight request: a thread that, once released, makes
    the request's final write to the store. A successful ``stop`` establishes
    termination (releases the request and joins its thread); a failed or
    blocked ``stop`` leaves it running.
    """

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.started = threading.Event()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()
        self.stop_entered = threading.Event()
        self.release_stop = threading.Event()
        self.release_request = threading.Event()
        self.gate_start = False
        self.fail_start = False
        self.gate_stop = False
        self.fail_stop = False
        self.request: threading.Thread | None = None

    def _serve_request(self) -> None:
        self.release_request.wait(WAIT)
        self.h.write("grpc_final")
        self.h.trace.append("grpc_final_write")

    async def start(self) -> None:
        self.h.trace.append("grpc_start")
        self.request = threading.Thread(target=self._serve_request, daemon=True)
        self.request.start()  # serving: a start failing after this is a partial start
        self.started.set()
        self.start_entered.set()
        if self.gate_start:
            await asyncio.to_thread(self.release_start.wait, WAIT)
        if self.fail_start:
            raise RuntimeError("grpc start failed")

    async def stop(self, grace: float | None = None) -> None:
        self.h.trace.append("grpc_stop")
        self.stop_entered.set()
        if self.gate_stop:
            await asyncio.to_thread(self.release_stop.wait, WAIT)
        if self.fail_stop:
            raise RuntimeError("grpc stop failed")
        self.release_request.set()
        await asyncio.to_thread(self.join)
        self.h.trace.append("grpc_terminated")

    def release(self) -> None:
        for gate in (self.release_start, self.release_stop, self.release_request):
            gate.set()

    def join(self) -> None:
        if self.request is not None:
            self.request.join(WAIT)


@pytest.fixture
def grpc_edge(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGrpcEdge]:
    import engine.grpc.server as grpc_server_module

    h = harness
    h.settings.webhooks_enabled = False  # no webhook guard can mask the gRPC state
    h.settings.grpc_enabled = True
    edge = FakeGrpcEdge(h)

    async def create(*args: Any, **kwargs: Any) -> tuple[FakeGrpcEdge, int]:
        return edge, 0

    monkeypatch.setattr(grpc_server_module, "create_grpc_server", create)
    h.extra_releases.append(edge.release)
    try:
        yield edge
    finally:
        edge.release()
        edge.join()


class Events:
    """Warnings logged by the engine, with their safe ``writers``/``reason``."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    def names(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def retained_writers(self) -> list[str]:
        for r in self.records:
            if r.getMessage() == "store_ownership_retained":
                return str(getattr(r, "writers", "")).split(",")
        return []


@pytest.fixture
def events() -> Iterator[Events]:
    captured = Events()

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.records.append(record)

    # create_app resets the root logger's handlers, so listen on the engine's logger.
    startup_logger = logging.getLogger("engine.startup")
    handler = _Capture(level=logging.WARNING)
    startup_logger.addHandler(handler)
    try:
        yield captured
    finally:
        startup_logger.removeHandler(handler)


def _teardown_task() -> asyncio.Task[Any]:
    [cleanup] = [
        t
        for t in asyncio.all_tasks()
        if getattr(t.get_coro(), "__qualname__", "").endswith("teardown")
    ]
    return cleanup


async def _outcome(lifespan: asyncio.Task[None]) -> BaseException | None:
    """The lifespan's outcome (``None`` for a normal return), bounded."""
    try:
        await asyncio.wait_for(asyncio.shield(lifespan), WAIT)
    except BaseException as exc:  # noqa: BLE001 - the outcome is what is tested
        return exc
    return None


def _assert_incomplete(outcome: BaseException | None) -> None:
    assert outcome is not None, "shutdown reported success although cleanup was cut short"
    assert type(outcome).__name__ == "ShutdownIncompleteError", repr(outcome)


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
    store stays owned (and says so) and shutdown reports that it is incomplete."""
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
            _teardown_task().cancel()
            _assert_incomplete(await _outcome(lifespan))
            # Shutdown ended, but the writer is still running: still owned.
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


# -- gRPC: termination must be established before release ----------------------


def test_a_failed_grpc_stop_keeps_both_locks(
    harness: Harness, grpc_edge: FakeGrpcEdge, events: Events
) -> None:
    h, edge = harness, grpc_edge
    edge.fail_stop = True

    async def main() -> Any:
        app, stop, lifespan = _start(h)
        try:
            assert await _wait(edge.started), "the gRPC edge never started"
            assert app.state.model_service.owns_work() is False  # no download guard
            assert h.settings.webhooks_enabled is False  # no webhook guard
            assert getattr(app.state, "store_ownership_retained", None) is None
            stop.set()
            outcome = await _outcome(lifespan)
            assert isinstance(outcome, RuntimeError) and "grpc stop failed" in str(outcome), (
                repr(outcome)
            )
            assert "telemetry_stop" in h.trace  # later cleanup steps still ran

            # The request can still write: both resources stay excluded.
            await _assert_owned(h, "after a failed gRPC stop")
            assert "grpc" in events.retained_writers(), events.names()
            assert getattr(app.state, "store_ownership_retained", None) is not None
            before = h.store_snapshot()
            assert await asyncio.to_thread(h.competing_engine_refused)
            assert h.store_snapshot() == before, "a refused competitor changed the store"
            assert h.final_writes() == 0

            # The request's final write lands while the store is still owned.
            edge.release_request.set()
            await asyncio.to_thread(edge.join)
            assert h.final_writes() == 1
            await _assert_owned(h, "after the request's final write")
        finally:
            h.release_all()
        return app

    app = asyncio.run(main())
    assert "store_released" not in h.trace
    # Fixture disposal of the test-held locks; not a production unlock policy.
    app.state.store_ownership_retained.release()
    assert h.database_free() and h.model_directory_free()


def test_cleanup_cancelled_before_the_grpc_stop_is_incomplete_and_retains(
    harness: Harness, grpc_edge: FakeGrpcEdge, events: Events
) -> None:
    h, edge = harness, grpc_edge

    async def main() -> Any:
        loop = asyncio.get_running_loop()

        def cut_short(loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> Any:
            task = asyncio.Task(coro, loop=loop, **kwargs)
            if getattr(coro, "__qualname__", "").endswith("teardown"):
                task.cancel()  # before its first step: gRPC stop is never reached
            return task

        app, stop, lifespan = _start(h)
        try:
            assert await _wait(edge.started)
            loop.set_task_factory(cut_short)
            stop.set()
            outcome = await _outcome(lifespan)
            loop.set_task_factory(None)
            _assert_incomplete(outcome)
            assert "grpc_stop" not in h.trace
            assert "shutdown_incomplete" in events.names()
            await _assert_owned(h, "cleanup cancelled before the gRPC stop")
            assert "grpc" in events.retained_writers(), events.names()
        finally:
            loop.set_task_factory(None)
            h.release_all()
        await asyncio.to_thread(edge.join)
        return app

    app = asyncio.run(main())
    assert "store_released" not in h.trace
    app.state.store_ownership_retained.release()  # fixture disposal
    assert h.database_free() and h.model_directory_free()


def test_cleanup_cancelled_during_the_grpc_stop_is_incomplete_and_retains(
    harness: Harness, grpc_edge: FakeGrpcEdge, events: Events
) -> None:
    h, edge = harness, grpc_edge
    edge.gate_stop = True

    async def main() -> Any:
        app, stop, lifespan = _start(h)
        try:
            assert await _wait(edge.started)
            stop.set()
            assert await _wait(edge.stop_entered), "cleanup never began the gRPC stop"
            _teardown_task().cancel()  # the stop began; termination is not established
            _assert_incomplete(await _outcome(lifespan))
            assert "shutdown_incomplete" in events.names()
            await _assert_owned(h, "cleanup cancelled during the gRPC stop")
            assert "grpc" in events.retained_writers(), events.names()
            assert h.final_writes() == 0
        finally:
            h.release_all()
        await asyncio.to_thread(edge.join)
        return app

    app = asyncio.run(main())
    assert "store_released" not in h.trace
    app.state.store_ownership_retained.release()  # fixture disposal
    assert h.database_free() and h.model_directory_free()


def test_repeated_outer_cancellation_waits_for_grpc_termination(
    harness: Harness, grpc_edge: FakeGrpcEdge
) -> None:
    h, edge = harness, grpc_edge
    edge.gate_stop = True

    async def main() -> None:
        _, stop, lifespan = _start(h)
        try:
            assert await _wait(edge.started)
            stop.set()
            assert await _wait(edge.stop_entered)
            for attempt in (1, 2):
                lifespan.cancel()
                assert await _still_running(lifespan), f"cancel {attempt} cut shutdown short"
                await _assert_owned(h, f"outer cancel {attempt} during the gRPC stop")
        finally:
            h.release_all()
        outcome = await _outcome(lifespan)
        assert isinstance(outcome, asyncio.CancelledError), repr(outcome)

    asyncio.run(main())
    order = h.trace
    assert order.index("grpc_final_write") < order.index("grpc_terminated"), order
    assert order.index("grpc_terminated") < order.index("telemetry_stop"), order
    assert order.index("telemetry_stop") < order.index("store_released"), order
    assert order.count("store_released") == 1
    assert h.database_free() and h.model_directory_free()


def test_caller_cancellation_wins_over_a_failed_grpc_stop_and_retains(
    harness: Harness, grpc_edge: FakeGrpcEdge, events: Events
) -> None:
    h, edge = harness, grpc_edge
    edge.gate_stop = True
    edge.fail_stop = True

    async def main() -> Any:
        app, stop, lifespan = _start(h)
        try:
            assert await _wait(edge.started)
            stop.set()
            assert await _wait(edge.stop_entered)
            lifespan.cancel()
            assert await _still_running(lifespan)
            edge.release_stop.set()  # the stop now fails
            outcome = await _outcome(lifespan)
            assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
            assert "shutdown_failed_while_cancelled" in events.names()
            await _assert_owned(h, "cancelled with a failed gRPC stop")
            assert "grpc" in events.retained_writers(), events.names()
        finally:
            h.release_all()
        await asyncio.to_thread(edge.join)
        return app

    app = asyncio.run(main())
    assert "store_released" not in h.trace
    app.state.store_ownership_retained.release()  # fixture disposal
    assert h.database_free() and h.model_directory_free()


def test_a_partial_grpc_start_is_stopped_before_release(
    harness: Harness, grpc_edge: FakeGrpcEdge
) -> None:
    h, edge = harness, grpc_edge
    edge.fail_start = True  # fails after it began serving

    async def main() -> None:
        _, _, lifespan = _start(h)
        outcome = await _outcome(lifespan)
        assert isinstance(outcome, RuntimeError) and "grpc start failed" in str(outcome)

    asyncio.run(main())
    order = h.trace
    assert order.index("grpc_final_write") < order.index("store_released"), order
    assert order.index("grpc_terminated") < order.index("store_released"), order
    assert h.database_free() and h.model_directory_free()


def test_a_cancelled_grpc_start_is_stopped_before_release(
    harness: Harness, grpc_edge: FakeGrpcEdge
) -> None:
    h, edge = harness, grpc_edge
    edge.gate_start = True

    async def main() -> None:
        _, _, lifespan = _start(h)
        try:
            assert await _wait(edge.start_entered)
            lifespan.cancel()  # startup is cancelled while the server is starting
        finally:
            edge.release_start.set()
        outcome = await _outcome(lifespan)
        assert isinstance(outcome, asyncio.CancelledError), repr(outcome)

    asyncio.run(main())
    order = h.trace
    assert "grpc_stop" in order, order
    assert order.index("grpc_terminated") < order.index("store_released"), order
    assert h.database_free() and h.model_directory_free()


def test_a_partial_grpc_start_whose_stop_fails_retains(
    harness: Harness, grpc_edge: FakeGrpcEdge, events: Events
) -> None:
    h, edge = harness, grpc_edge
    edge.fail_start = True
    edge.fail_stop = True

    async def main() -> Any:
        app, _, lifespan = _start(h)
        try:
            outcome = await _outcome(lifespan)
            assert isinstance(outcome, RuntimeError), repr(outcome)
            await _assert_owned(h, "partial start, termination unknown")
            assert "grpc" in events.retained_writers(), events.names()
        finally:
            h.release_all()
        await asyncio.to_thread(edge.join)
        return app

    app = asyncio.run(main())
    assert "store_released" not in h.trace
    app.state.store_ownership_retained.release()  # fixture disposal
    assert h.database_free() and h.model_directory_free()


def test_an_ordinary_failure_after_grpc_terminated_still_releases(
    harness: Harness, grpc_edge: FakeGrpcEdge, events: Events
) -> None:
    h = harness
    h.fail_drain = True  # a later step fails after gRPC termination was established

    async def main() -> Any:
        app, stop, lifespan = _start(h)
        assert await _wait(grpc_edge.started)
        stop.set()
        outcome = await _outcome(lifespan)
        assert isinstance(outcome, RuntimeError) and "drain failed" in str(outcome)
        return app

    app = asyncio.run(main())
    assert h.trace.index("grpc_terminated") < h.trace.index("store_released")
    assert h.trace.count("store_released") == 1
    assert getattr(app.state, "store_ownership_retained", None) is None
    assert "store_ownership_retained" not in events.names()
    assert h.database_free() and h.model_directory_free()


def test_grpc_disabled_shutdown_releases(harness: Harness) -> None:
    h = harness
    h.settings.webhooks_enabled = False
    assert h.settings.grpc_enabled is False

    async def main() -> Any:
        app, stop, lifespan = _start(h)
        await asyncio.sleep(0)
        stop.set()
        assert await _outcome(lifespan) is None
        return app

    app = asyncio.run(main())
    assert h.trace.count("store_released") == 1
    assert getattr(app.state, "store_ownership_retained", None) is None
    assert h.database_free() and h.model_directory_free()
