"""Block 7 edge behavior under load: cancellation, responsiveness, saturation.

Driven through a real Uvicorn server (client disconnects need real sockets).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID, running_app
from tests.support.fake_backend import FakeBackend


def _app(tmp_path: Path, **settings: object):
    fake = FakeBackend(tokens=["x"] * 200, per_token_delay=0.01, model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(Settings(data_dir=tmp_path, **settings), backend=fake)


def test_cancellation_frees_capacity(tmp_path: Path) -> None:
    """A disconnected client's slot is freed so the next request runs."""
    app = _app(tmp_path)

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Open a stream, read one token, then abandon it (client disconnect).
            async with client.stream(
                "POST",
                f"{base}/v1/chat/completions",
                json={
                    "model": MODEL_ID,
                    "messages": [{"role": "user", "content": "a"}],
                    "stream": True,
                },
            ) as s:
                assert s.status_code == 200
                async for line in s.aiter_lines():
                    if line.startswith("data: ") and '"content"' in line:
                        break
                # Leaving the context here closes the connection mid-generation.

            # Give the server a moment to observe the disconnect and free the slot.
            for _ in range(100):
                sched = (await client.get(f"{base}/metrics")).json()["scheduler"]
                if sched["in_use"] == 0:
                    break
                await asyncio.sleep(0.02)
            assert sched["cancelled_total"] >= 1
            assert sched["in_use"] == 0

            # Capacity is freed, and the engine recovers: a fresh request succeeds.
            # There is a brief, honest window where the backend is still finishing
            # the aborted generation (the model stays busy until the in-flight call
            # returns), during which a new request gets a retriable 503 model_busy —
            # so retry briefly, exactly as a real client would.
            status = None
            for _ in range(100):
                done = await client.post(
                    f"{base}/v1/chat/completions",
                    json={
                        "model": MODEL_ID,
                        "messages": [{"role": "user", "content": "b"}],
                        "max_tokens": 3,
                    },
                )
                status = done.status_code
                if status == 200:
                    break
                assert done.json()["error"]["code"] == "model_busy"
                await asyncio.sleep(0.02)
            assert status == 200, done.text

    with running_app(app) as base:
        asyncio.run(body(base))


def test_health_and_metrics_responsive_under_load(tmp_path: Path) -> None:
    """Operator endpoints stay fast while many generations are queued/running."""
    app = _app(tmp_path)  # single slot; the rest queue

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:

            async def stream_one(msg: str) -> None:
                try:
                    async with client.stream(
                        "POST",
                        f"{base}/v1/chat/completions",
                        json={
                            "model": MODEL_ID,
                            "messages": [{"role": "user", "content": msg}],
                            "stream": True,
                        },
                    ) as s:
                        async for _ in s.aiter_lines():
                            pass
                except httpx.HTTPError:
                    pass

            # Saturate: one runs, the others wait in the admission queue.
            load = [asyncio.create_task(stream_one(str(i))) for i in range(8)]
            await asyncio.sleep(0.1)  # let the queue build up

            # Health + metrics must answer promptly despite the backlog.
            for path in ("/healthz", "/metrics"):
                start = time.monotonic()
                resp = await client.get(f"{base}{path}")
                elapsed = time.monotonic() - start
                assert resp.status_code == 200
                assert elapsed < 1.0, f"{path} took {elapsed:.3f}s under load"

            sched = (await client.get(f"{base}/metrics")).json()["scheduler"]
            assert sched["queue_depth"] >= 1  # requests really were queued

            for t in load:
                t.cancel()
            await asyncio.gather(*load, return_exceptions=True)

    with running_app(app) as base:
        asyncio.run(body(base))
