"""Admission control at the edge (Block 7).

The Block 1 backend still serializes generation (one model context), but the
scheduler now *queues* overlapping requests instead of rejecting them, and only
sheds load with an explicit, retriable 429 once the bounded queue is full.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID, running_app
from tests.support.fake_backend import FakeBackend


def _app(tmp_path: Path, **settings: object):
    fake = FakeBackend(tokens=["x"] * 40, per_token_delay=0.01, model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(Settings(data_dir=tmp_path, **settings), backend=fake)


def test_second_request_queues_then_succeeds(tmp_path: Path) -> None:
    """With queue capacity, an overlapping request waits and then completes."""
    app = _app(tmp_path)  # defaults: max_concurrency=1, queue depth 32

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Start a long streaming generation and hold it open in the background.
            async def stream_a() -> None:
                async with client.stream(
                    "POST",
                    f"{base}/v1/chat/completions",
                    json={
                        "model": MODEL_ID,
                        "messages": [{"role": "user", "content": "a"}],
                        "stream": True,
                    },
                ) as s:
                    async for _ in s.aiter_lines():
                        pass

            task = asyncio.create_task(stream_a())
            await asyncio.sleep(0.05)  # let A occupy the single slot

            # B overlaps A. It must queue (not be rejected) and then succeed.
            second = await client.post(
                f"{base}/v1/chat/completions",
                json={"model": MODEL_ID, "messages": [{"role": "user", "content": "b"}]},
            )
            assert second.status_code == 200, second.text
            await task

            sched = (await client.get(f"{base}/metrics")).json()["scheduler"]
            assert sched["admitted_total"] >= 2
            assert sched["rejected_total"] == 0
            # B had to wait for A's slot, so the queue was used.
            assert sched["peak_queue_depth"] >= 1

    with running_app(app) as base:
        asyncio.run(body(base))


def test_saturation_returns_429(tmp_path: Path) -> None:
    """With no queue capacity, an overlapping request is shed with a 429."""
    app = _app(tmp_path, max_concurrency_queue=0)

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:

            async def stream_a() -> None:
                async with client.stream(
                    "POST",
                    f"{base}/v1/chat/completions",
                    json={
                        "model": MODEL_ID,
                        "messages": [{"role": "user", "content": "a"}],
                        "stream": True,
                    },
                ) as s:
                    async for _ in s.aiter_lines():
                        pass

            task = asyncio.create_task(stream_a())
            await asyncio.sleep(0.05)

            second = await client.post(
                f"{base}/v1/chat/completions",
                json={"model": MODEL_ID, "messages": [{"role": "user", "content": "b"}]},
            )
            assert second.status_code == 429
            assert second.json()["error"]["code"] == "engine_saturated"
            assert second.headers.get("retry-after") is not None
            await task

            sched = (await client.get(f"{base}/metrics")).json()["scheduler"]
            assert sched["rejected_queue_full"] >= 1

    with running_app(app) as base:
        asyncio.run(body(base))
