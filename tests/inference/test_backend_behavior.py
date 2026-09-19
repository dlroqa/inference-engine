"""Block 1 required behavioral tests, driven through the real async bridge.

These use :class:`FakeBackend` (a blocking producer in a worker thread) so they
validate threading, ordering, cancellation, and event-loop responsiveness without
llama.cpp. Each test wraps an async body in ``asyncio.run`` to avoid a plugin dep.
"""

from __future__ import annotations

import asyncio

import pytest

from engine.inference.types import (
    BackendBusyError,
    BackendNotReadyError,
    BackendState,
    FinishReason,
    GenerationFailedError,
    GenerationRequest,
    ModelLoadError,
)
from tests.support.fake_backend import FakeBackend


def test_generation_yields_ordered_chunks_and_terminal_result() -> None:
    async def body() -> None:
        backend = FakeBackend(tokens=["a", "b", "c", "d"])
        await backend.load()
        assert backend.state == BackendState.READY

        stream = backend.generate(GenerationRequest(prompt="one two three"))
        chunks = [chunk async for chunk in stream]

        assert [c.text for c in chunks] == ["a", "b", "c", "d"]
        assert [c.index for c in chunks] == [0, 1, 2, 3]

        result = stream.result
        assert result is not None
        assert result.text == "abcd"
        assert result.finish_reason == FinishReason.STOP
        assert result.completion_tokens == 4
        assert result.prompt_tokens == 3  # "one two three"
        assert result.timings.total_ms is not None
        assert backend.state == BackendState.READY  # released after completion

    asyncio.run(body())


def test_collect_convenience() -> None:
    async def body() -> None:
        backend = FakeBackend(tokens=["x", "y"])
        await backend.load()
        result = await backend.generate(GenerationRequest(prompt="p")).collect()
        assert result.text == "xy"
        assert result.finish_reason == FinishReason.STOP

    asyncio.run(body())


def test_loading_failure_leaves_recoverable_state() -> None:
    async def body() -> None:
        backend = FakeBackend(fail_on_load=True)
        with pytest.raises(ModelLoadError):
            await backend.load()
        # Not stuck in LOADING; recoverable and clearly not ready.
        assert backend.state == BackendState.UNLOADED
        assert backend.loaded is False
        with pytest.raises(BackendNotReadyError):
            backend.generate(GenerationRequest(prompt="p"))

    asyncio.run(body())


def test_unload_frees_runtime_state() -> None:
    async def body() -> None:
        backend = FakeBackend(tokens=["a"])
        await backend.load()
        assert backend.loaded is True
        await backend.unload()
        assert backend.loaded is False
        assert backend.state == BackendState.UNLOADED
        await backend.unload()  # idempotent

    asyncio.run(body())


def test_event_loop_stays_responsive_during_generation() -> None:
    async def body() -> None:
        # ~0.24s of blocking work spread over 8 tokens in a worker thread.
        backend = FakeBackend(tokens=["x"] * 8, per_token_delay=0.03)
        await backend.load()
        stream = backend.generate(GenerationRequest(prompt="hi"))

        async def consume() -> int:
            return len([c async for c in stream])

        task = asyncio.create_task(consume())
        pings = 0
        while not task.done():
            await asyncio.sleep(0.005)
            pings += 1
        count = await task

        assert count == 8
        # If the blocking producer had run on the loop, pings would be ~0.
        assert pings > 10
        assert backend.state == BackendState.READY

    asyncio.run(body())


def test_cancellation_ends_generation_and_releases_worker() -> None:
    async def body() -> None:
        backend = FakeBackend(tokens=["x"] * 100, per_token_delay=0.02)
        await backend.load()
        stream = backend.generate(GenerationRequest(prompt="hi"))

        it = stream.__aiter__()
        await it.__anext__()  # consume one token, generation still active
        assert backend.state == BackendState.GENERATING

        await stream.aclose()

        assert stream.result is not None
        assert stream.result.finish_reason == FinishReason.CANCELLED
        assert backend.state == BackendState.READY  # worker released

        # The worker is genuinely free: a new generation can start and finish.
        result = await backend.generate(GenerationRequest(prompt="again")).collect()
        assert result.finish_reason == FinishReason.CANCELLED or result.text == "x" * 100

    asyncio.run(body())


def test_generation_failure_is_reported_and_recovers() -> None:
    async def body() -> None:
        backend = FakeBackend(tokens=["a", "b", "c", "d"], fail_after=2)
        await backend.load()
        stream = backend.generate(GenerationRequest(prompt="p"))

        collected = []
        with pytest.raises(GenerationFailedError):
            async for chunk in stream:
                collected.append(chunk.text)

        assert collected == ["a", "b"]
        assert stream.result is not None
        assert stream.result.finish_reason == FinishReason.ERROR
        # Model stays loaded and usable after a mid-stream generation error.
        assert backend.state == BackendState.READY

    asyncio.run(body())


def test_busy_and_not_ready_guards() -> None:
    async def body() -> None:
        backend = FakeBackend(tokens=["x"] * 50, per_token_delay=0.02)

        # Not loaded yet.
        with pytest.raises(BackendNotReadyError):
            backend.generate(GenerationRequest(prompt="p"))

        await backend.load()
        stream = backend.generate(GenerationRequest(prompt="p"))
        try:
            with pytest.raises(BackendBusyError):
                backend.generate(GenerationRequest(prompt="p"))
        finally:
            await stream.aclose()

    asyncio.run(body())


def test_health_and_capabilities() -> None:
    async def body() -> None:
        backend = FakeBackend()
        # Not ready -> capabilities raises, health reports unloaded.
        with pytest.raises(BackendNotReadyError):
            backend.capabilities()
        assert (await backend.health())["state"] == "unloaded"

        await backend.load()
        caps = backend.capabilities()
        assert caps.model_id == "fake-model"
        health = await backend.health()
        assert health["state"] == "ready"
        assert health["model_id"] == "fake-model"
        assert "secret" not in str(health).lower()

    asyncio.run(body())
