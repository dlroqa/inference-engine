"""A disconnected streaming client cancels the internal generation.

Uses a real Uvicorn server so an early client disconnect actually propagates
(the in-process ASGI transport buffers the stream and cannot).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from engine.config import Settings
from engine.inference.types import BackendState
from engine.main import create_app
from tests.api.conftest import MODEL_ID, running_app
from tests.support.fake_backend import FakeBackend


def test_client_disconnect_cancels_generation(tmp_path: Path) -> None:
    fake = FakeBackend(tokens=["x"] * 1000, per_token_delay=0.02, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(Settings(data_dir=tmp_path), backend=fake)

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                f"{base}/v1/chat/completions",
                json={
                    "model": MODEL_ID,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                assert fake.state == BackendState.GENERATING
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and '"content"' in line:
                        break  # got a token; leaving the context disconnects

        # After disconnect the worker must be released, not stuck GENERATING.
        for _ in range(100):
            if fake.state == BackendState.READY:
                break
            await asyncio.sleep(0.05)
        assert fake.state == BackendState.READY

    with running_app(app) as base:
        asyncio.run(body(base))
