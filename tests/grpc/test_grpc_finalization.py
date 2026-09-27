"""A gRPC request is finalized exactly once, even when cancellation interrupts
the closing of its generation stream.

These drive the real servicer (``Generate``) against a real app, backend stream,
scheduler, backend registry, counters and a temporary store, the way
``grpc.aio`` drives a streaming handler from its own task. The only seam is the
stream's ``aclose``, which can signal entry and wait at a barrier (or fail).
``finish_generation`` is spied on by delegating to the real finalizer, so its
effects are asserted directly: scheduler and backend capacity released, request
counters settled, and one ``usage_events`` row in the store. Every gate is
released and every stream really closed in ``finally``.
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

import engine.grpc.server as grpc_server_module
from engine.config import Settings
from engine.grpc import inference_pb2 as pb
from engine.inference.stream import QueueGenerationStream
from engine.inference.types import FinishReason, GenerationRequest
from engine.main import create_app
from engine.quota.store import UsageStore
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend

WAIT = 30  # seconds; upper bound for every blocking wait
TOKENS = ["one", " two", " three"]


class HeldBackend(FakeBackend):
    """Emits the first token, then holds the stream until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__(tokens=TOKENS, model_id=MODEL_ID)
        self.release = threading.Event()

    def _token_producer(self, request: GenerationRequest, cancel: threading.Event) -> Any:
        yield TOKENS[0]
        while not self.release.wait(0.01):
            if cancel.is_set():
                return FinishReason.CANCELLED
        yield from TOKENS[1:]
        return FinishReason.STOP


class CloseSeam:
    """Controls ``QueueGenerationStream.aclose``: gate it, or make it fail."""

    def __init__(self) -> None:
        self.gate = False
        self.fail = False
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.streams: list[QueueGenerationStream] = []
        self.real_aclose: Any = None

    def arm(self) -> None:  # events belong to the running loop
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def open(self) -> None:
        if self.release is not None:
            self.release.set()


class _Context:
    """The parts of a servicer context that ``Generate`` uses."""

    def invocation_metadata(self) -> tuple[()]:
        return ()

    async def abort(self, code: Any, details: str) -> None:
        raise AssertionError(f"aborted: {code} {details}")


class Engine:
    """A real app with its lifespan running, plus the gRPC edge (not started)."""

    def __init__(self, tmp_path: Path, backend: FakeBackend) -> None:
        asyncio.run(backend.load())
        self.backend = backend
        self.settings = Settings(data_dir=tmp_path / "data")
        self.app = create_app(self.settings, backend=backend)
        self.finalized: list[BaseException | None] = []

    def usage_rows(self) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(str(self.settings.db_path))
        try:
            return conn.execute(
                "SELECT key_id, endpoint, status FROM usage_events WHERE endpoint = ?",
                ("/grpc/Generate",),
            ).fetchall()
        finally:
            conn.close()

    def assert_settled(self) -> None:
        """The request's capacity is released and its accounting done."""
        assert self.app.state.scheduler.snapshot()["in_use"] == 0, "scheduler slot held"
        entries = self.app.state.backend_registry.entries
        assert all(e.in_flight == 0 for e in entries), "backend in-flight slot held"
        counters = self.app.state.counters.snapshot()
        assert counters.requests_active == 0, "request still counted as active"
        assert counters.requests_total == 1
        assert len(self.usage_rows()) == 1, self.usage_rows()


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[CloseSeam]:
    control = CloseSeam()
    real_aclose = QueueGenerationStream.aclose
    control.real_aclose = real_aclose

    async def aclose(self: QueueGenerationStream) -> None:
        control.streams.append(self)
        if control.gate and control.entered is not None and control.release is not None:
            control.entered.set()
            await control.release.wait()
        if control.fail:
            await real_aclose(self)
            raise RuntimeError("stream close failed")
        await real_aclose(self)

    monkeypatch.setattr(QueueGenerationStream, "aclose", aclose)
    yield control


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[BaseException | None]]:
    calls: list[BaseException | None] = []
    real_finish = grpc_server_module.finish_generation

    def finish(served: Any, *, error: BaseException | None) -> Any:
        calls.append(error)
        return real_finish(served, error=error)

    monkeypatch.setattr(grpc_server_module, "finish_generation", finish)
    yield calls


def _request() -> Any:
    return pb.GenerateRequest(
        model=MODEL_ID, messages=[pb.Message(role="user", content="hi")], max_tokens=16
    )


async def _cleanup(seam: CloseSeam, backend: FakeBackend) -> None:
    """Unblock and really close everything the test started (fixture disposal)."""
    seam.gate = False
    seam.fail = False
    seam.open()
    if isinstance(backend, HeldBackend):
        backend.release.set()
    for stream in seam.streams:
        await asyncio.wait_for(seam.real_aclose(stream), WAIT)


def _run(engine: Engine, body: Any) -> None:
    async def main() -> None:
        async with engine.app.router.lifespan_context(engine.app):
            edge, _ = await grpc_server_module.create_grpc_server(
                engine.app, host="127.0.0.1", port=0
            )
            await body(edge)

    asyncio.run(main())


def test_cancellation_while_closing_a_completed_stream_still_finalizes_once(
    tmp_path: Path, seam: CloseSeam, spy: list[BaseException | None]
) -> None:
    engine = Engine(tmp_path, FakeBackend(tokens=TOKENS, model_id=MODEL_ID))

    async def body(edge: Any) -> None:
        seam.arm()
        seam.gate = True
        generator = edge.servicer.Generate(_request(), _Context())
        received: list[Any] = []

        async def call() -> None:  # drives the handler the way grpc.aio does
            async for chunk in generator:
                received.append(chunk)

        task = asyncio.create_task(call())
        try:
            assert seam.entered is not None
            await asyncio.wait_for(seam.entered.wait(), WAIT)  # every token consumed
            assert [c.text for c in received] == TOKENS
            task.cancel()  # interrupts the awaited stream close
            [outcome] = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(outcome, asyncio.CancelledError), repr(outcome)

            assert len(spy) == 1, "the request was not finalized (or finalized twice)"
            assert spy[0] is None  # interrupted closing is not a generation error
            engine.assert_settled()

            # Later closure of the handler and the edge adds nothing.
            await generator.aclose()
            await asyncio.wait_for(edge.stop(grace=0), WAIT)
            assert len(spy) == 1
            engine.assert_settled()
        finally:
            await _cleanup(seam, engine.backend)
            await asyncio.gather(task, return_exceptions=True)

    _run(engine, body)


def test_a_disconnect_then_cancellation_while_closing_finalizes_once(
    tmp_path: Path, seam: CloseSeam, spy: list[BaseException | None]
) -> None:
    engine = Engine(tmp_path, HeldBackend())

    async def body(edge: Any) -> None:
        seam.arm()
        seam.gate = True
        generator = edge.servicer.Generate(_request(), _Context())
        first = asyncio.Event()

        async def call() -> None:
            async for _ in generator:
                first.set()

        task = asyncio.create_task(call())
        try:
            # The handler yielded its first chunk and, in the same step, went on
            # to wait for the next token, which the backend holds.
            await asyncio.wait_for(first.wait(), WAIT)
            task.cancel()  # the client goes away mid-stream
            assert seam.entered is not None
            await asyncio.wait_for(seam.entered.wait(), WAIT)  # its cleanup is closing
            task.cancel()  # and that close is cancelled too
            [outcome] = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(outcome, asyncio.CancelledError), repr(outcome)

            assert len(spy) == 1, "the request was not finalized (or finalized twice)"
            assert spy[0] is None  # a disconnect, not a generation error
            engine.assert_settled()
            assert engine.app.state.counters.snapshot().requests_errors == 0

            await generator.aclose()
            await asyncio.wait_for(edge.stop(grace=0), WAIT)
            assert len(spy) == 1
            engine.assert_settled()
        finally:
            await _cleanup(seam, engine.backend)
            await asyncio.gather(task, return_exceptions=True)

    _run(engine, body)


def test_normal_completion_finalizes_once_and_a_late_close_is_harmless(
    tmp_path: Path, seam: CloseSeam, spy: list[BaseException | None]
) -> None:
    engine = Engine(tmp_path, FakeBackend(tokens=TOKENS, model_id=MODEL_ID))

    async def body(edge: Any) -> None:
        generator = edge.servicer.Generate(_request(), _Context())
        try:
            chunks = [c async for c in generator]
            terminal = [c for c in chunks if c.done]
            assert len(terminal) == 1 and terminal[0].finish_reason == "stop"
            assert terminal[0].completion_tokens == len(TOKENS)
            assert spy == [None]
            engine.assert_settled()

            await generator.aclose()
            await asyncio.wait_for(edge.stop(grace=0), WAIT)
            assert spy == [None]
            engine.assert_settled()
        finally:
            await _cleanup(seam, engine.backend)

    _run(engine, body)


def test_an_ordinary_close_failure_is_best_effort_and_still_finalizes(
    tmp_path: Path, seam: CloseSeam, spy: list[BaseException | None]
) -> None:
    engine = Engine(tmp_path, FakeBackend(tokens=TOKENS, model_id=MODEL_ID))

    async def body(edge: Any) -> None:
        seam.fail = True
        generator = edge.servicer.Generate(_request(), _Context())
        try:
            chunks = [c async for c in generator]
            assert sum(1 for c in chunks if c.done) == 1  # the terminal chunk still goes out
            assert spy == [None]
            engine.assert_settled()
        finally:
            await _cleanup(seam, engine.backend)

    _run(engine, body)


@pytest.fixture
def grpc_warnings() -> Iterator[list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("engine.grpc")
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize("cancelled", [False, True], ids=["plain", "while-cancelled"])
def test_a_failing_finalizer_is_reported_and_not_retried(
    tmp_path: Path,
    seam: CloseSeam,
    spy: list[BaseException | None],
    grpc_warnings: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    """The usage write fails. Without cancellation the failure reaches the
    handler; with it, cancellation wins and the failure is logged by type. In
    both cases the finalizer ran once and later closes do not run it again."""
    engine = Engine(tmp_path, FakeBackend(tokens=TOKENS, model_id=MODEL_ID))

    def failing_record(self: UsageStore, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(UsageStore, "record", failing_record)

    async def body(edge: Any) -> None:
        seam.arm()
        seam.gate = cancelled
        generator = edge.servicer.Generate(_request(), _Context())

        async def call() -> None:
            async for _ in generator:
                pass

        task = asyncio.create_task(call())
        try:
            if cancelled:
                assert seam.entered is not None
                await asyncio.wait_for(seam.entered.wait(), WAIT)
                task.cancel()
            [outcome] = await asyncio.gather(task, return_exceptions=True)
            assert len(spy) == 1, "the finalizer did not run exactly once"
            if cancelled:
                assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
                names = [r.getMessage() for r in grpc_warnings]
                assert "grpc_request_finalize_failed" in names, names
            else:
                assert isinstance(outcome, sqlite3.OperationalError), repr(outcome)
            assert engine.usage_rows() == []  # nothing claims a durable record
            assert engine.app.state.scheduler.snapshot()["in_use"] == 0

            await generator.aclose()
            await asyncio.wait_for(edge.stop(grace=0), WAIT)
            assert len(spy) == 1, "a later close retried the finalizer"
        finally:
            await _cleanup(seam, engine.backend)
            await asyncio.gather(task, return_exceptions=True)

    _run(engine, body)
