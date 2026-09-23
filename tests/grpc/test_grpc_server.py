"""gRPC edge end to end (Block 10, sub-slice 6).

Runs the app (which starts the gRPC server in its lifespan on an auto port) via the
shared ``running_app`` helper, then drives it with a real gRPC client. Each async
body is wrapped in ``asyncio.run`` (no plugin dep), matching the rest of the suite.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from engine.config import Settings
from engine.grpc import inference_pb2 as pb
from engine.grpc import inference_pb2_grpc as pb_grpc
from engine.main import create_app
from tests.api.conftest import MODEL_ID, running_app
from tests.support.fake_backend import FakeBackend

TOKENS = ["Hello", ", ", "world", "!"]


def _loaded() -> FakeBackend:
    fake = FakeBackend(tokens=TOKENS, model_id=MODEL_ID)
    asyncio.run(fake.load())
    return fake


def _app(**overrides: object):
    settings = Settings(grpc_enabled=True, grpc_port=0, **overrides)  # type: ignore[arg-type]
    return create_app(settings, backend=_loaded())


def _chat_req(model: str = MODEL_ID) -> pb.GenerateRequest:
    return pb.GenerateRequest(
        model=model, messages=[pb.Message(role="user", content="hi")], max_tokens=16
    )


def test_generate_streams_tokens_and_terminal_chunk() -> None:
    app = _app()
    with running_app(app):
        port = app.state.grpc_port

        async def body() -> None:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
                stub = pb_grpc.InferenceServiceStub(ch)
                chunks = [c async for c in stub.Generate(_chat_req())]
                # deltas then exactly one terminal chunk
                deltas = [c.text for c in chunks if not c.done]
                terminals = [c for c in chunks if c.done]
                assert "".join(deltas) == "".join(TOKENS)
                assert len(terminals) == 1
                assert terminals[0].finish_reason == "stop"
                assert terminals[0].completion_tokens == len(TOKENS)

        asyncio.run(body())


def test_list_models() -> None:
    app = _app()
    with running_app(app):
        port = app.state.grpc_port

        async def body() -> None:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
                stub = pb_grpc.InferenceServiceStub(ch)
                resp = await stub.ListModels(pb.ListModelsRequest())
                assert list(resp.models) == [MODEL_ID]

        asyncio.run(body())


def test_unknown_model_is_not_found() -> None:
    app = _app()
    with running_app(app):
        port = app.state.grpc_port

        async def body() -> None:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
                stub = pb_grpc.InferenceServiceStub(ch)
                with pytest.raises(grpc.aio.AioRpcError) as exc:
                    async for _ in stub.Generate(_chat_req(model="nope")):
                        pass
                assert exc.value.code() == grpc.StatusCode.NOT_FOUND

        asyncio.run(body())


def test_saturation_is_resource_exhausted() -> None:
    app = _app()
    with running_app(app):
        port = app.state.grpc_port
        # Saturate the only backend's capacity so admission sheds.
        entry = app.state.backend_registry.entries[0]
        entry.in_flight = entry.max_in_flight

        async def body() -> None:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
                stub = pb_grpc.InferenceServiceStub(ch)
                with pytest.raises(grpc.aio.AioRpcError) as exc:
                    async for _ in stub.Generate(_chat_req()):
                        pass
                assert exc.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

        asyncio.run(body())


def test_auth_required_rejects_missing_key_and_accepts_valid() -> None:
    app = _app(require_auth=True)
    with running_app(app):
        port = app.state.grpc_port
        _record, token = app.state.gateway.keys.create("grpc-test")

        async def body() -> None:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
                stub = pb_grpc.InferenceServiceStub(ch)
                # No credentials -> UNAUTHENTICATED
                with pytest.raises(grpc.aio.AioRpcError) as exc:
                    async for _ in stub.Generate(_chat_req()):
                        pass
                assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
                # Valid key in metadata -> succeeds
                md = [("authorization", f"Bearer {token}")]
                chunks = [c async for c in stub.Generate(_chat_req(), metadata=md)]
                assert "".join(c.text for c in chunks if not c.done) == "".join(TOKENS)

        asyncio.run(body())
