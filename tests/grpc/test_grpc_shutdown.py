"""Shutdown with a real gRPC server and an in-flight streaming request.

The engine lifespan runs in the test's event loop with a real ``grpc.aio``
server and client. The backend holds the stream after its first token until the
test releases it, so shutdown begins while the handler is live. The store stays
owned until the handler's final usage write (``finish_generation``), the gRPC
stop, and the rest of cleanup have happened, and is released exactly once.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import grpc
import pytest

import engine.grpc.server as grpc_server_module
from engine.config import Settings
from engine.grpc import inference_pb2 as pb
from engine.grpc import inference_pb2_grpc as pb_grpc
from engine.inference.types import FinishReason, GenerationRequest
from engine.main import create_app
from engine.store.ownership import StoreLockedError, StoreOwnership
from engine.telemetry.service import Telemetry
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


def _free(db: Path, models: Path) -> bool:
    probe = StoreOwnership(db, models)
    try:
        probe.acquire()
    except StoreLockedError:
        return False
    probe.release()
    return True


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    events: list[str] = []
    real_finish = grpc_server_module.finish_generation
    real_telemetry_stop = Telemetry.stop
    real_release = StoreOwnership.release

    def finish(served: Any, *, error: BaseException | None) -> Any:
        result = real_finish(served, error=error)
        events.append("grpc_finalized")
        return result

    async def telemetry_stop(self: Telemetry) -> None:
        events.append("telemetry_stop")
        await real_telemetry_stop(self)

    def release(self: StoreOwnership) -> None:
        events.append("store_released")
        real_release(self)

    monkeypatch.setattr(grpc_server_module, "finish_generation", finish)
    monkeypatch.setattr(Telemetry, "stop", telemetry_stop)
    monkeypatch.setattr(StoreOwnership, "release", release)
    yield events


def test_shutdown_finalizes_an_in_flight_grpc_request_before_release(
    tmp_path: Path, trace: list[str]
) -> None:
    backend = HeldBackend()
    asyncio.run(backend.load())
    settings = Settings(data_dir=tmp_path / "data", grpc_enabled=True, grpc_port=0)
    app = create_app(settings, backend=backend)
    db, models = Path(settings.db_path), Path(settings.models_dir)
    other_db, other_models = tmp_path / "other.db", tmp_path / "other-models"

    async def main() -> list[Any]:
        stop = asyncio.Event()

        async def serve() -> None:
            async with app.router.lifespan_context(app):
                await stop.wait()

        lifespan = asyncio.create_task(serve())
        chunks: list[Any] = []
        try:
            for _ in range(WAIT * 100):
                if getattr(app.state, "grpc_port", None) or lifespan.done():
                    break
                await asyncio.sleep(0.01)
            port = app.state.grpc_port
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = pb_grpc.InferenceServiceStub(channel)
                call = stub.Generate(
                    pb.GenerateRequest(
                        model=MODEL_ID,
                        messages=[pb.Message(role="user", content="hi")],
                        max_tokens=16,
                    )
                )
                chunks.append(await asyncio.wait_for(call.read(), WAIT))  # handler is live

                stop.set()  # shutdown begins with the request in flight
                done, _ = await asyncio.wait({lifespan}, timeout=0.3)
                assert not done, "shutdown finished while a gRPC request was live"
                assert not await asyncio.to_thread(_free, db, other_models)
                assert not await asyncio.to_thread(_free, other_db, models)
                assert "grpc_finalized" not in trace

                backend.release.set()  # let the handler finish within the grace period
                while True:
                    chunk = await asyncio.wait_for(call.read(), WAIT)
                    if chunk is grpc.aio.EOF:
                        break
                    chunks.append(chunk)
            await asyncio.wait_for(lifespan, WAIT)
        finally:
            backend.release.set()
            stop.set()
        return chunks

    chunks = asyncio.run(main())
    terminal = [c for c in chunks if c.done]
    assert len(terminal) == 1 and terminal[0].finish_reason == "stop", chunks
    assert "".join(c.text for c in chunks if not c.done) == "".join(TOKENS)
    assert trace.count("grpc_finalized") == 1
    assert trace.index("grpc_finalized") < trace.index("telemetry_stop"), trace
    assert trace.index("telemetry_stop") < trace.index("store_released"), trace
    assert trace.count("store_released") == 1
    assert _free(db, other_models) and _free(other_db, models)


class _Context:
    """The parts of a servicer context that ``Generate`` uses."""

    def invocation_metadata(self) -> tuple[()]:
        return ()

    async def abort(self, code: Any, details: str) -> None:
        raise AssertionError(f"aborted: {code} {details}")


def test_edge_stop_finalizes_a_handler_abandoned_at_a_yield(
    tmp_path: Path, trace: list[str]
) -> None:
    """``grpc.aio`` awaits each write outside the handler's generator. A call
    cancelled during a write leaves the generator suspended at a ``yield``, so
    the request is not finalized; ``Server.stop()`` does not wait for that, but
    the edge's stop finalizes it before returning."""
    backend = FakeBackend(tokens=TOKENS, model_id=MODEL_ID)
    asyncio.run(backend.load())
    app = create_app(Settings(data_dir=tmp_path / "data"), backend=backend)
    request = pb.GenerateRequest(
        model=MODEL_ID, messages=[pb.Message(role="user", content="hi")], max_tokens=16
    )

    async def main() -> None:
        async with app.router.lifespan_context(app):
            edge, _ = await grpc_server_module.create_grpc_server(app, host="127.0.0.1", port=0)
            generator = edge.servicer.Generate(request, _Context())  # kept alive, like a frame
            first = asyncio.Event()

            async def call() -> None:  # drives the handler the way grpc.aio does
                await generator.__anext__()
                first.set()
                await asyncio.Event().wait()  # a write that never completes

            task = asyncio.create_task(call())
            await asyncio.wait_for(first.wait(), WAIT)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert "grpc_finalized" not in trace, "the abandoned call was finalized anyway"

            await asyncio.wait_for(edge.stop(grace=0), WAIT)
            assert trace.count("grpc_finalized") == 1

            await generator.aclose()  # a later finalization does not finish it twice
            assert trace.count("grpc_finalized") == 1

    asyncio.run(main())
