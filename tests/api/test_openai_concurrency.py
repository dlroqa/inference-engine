"""A second concurrent generation is rejected (single-model, Block 2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID, running_app
from tests.support.fake_backend import FakeBackend


def test_busy_returns_503(tmp_path: Path) -> None:
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
            ) as streaming:
                assert streaming.status_code == 200
                # Wait for the first token so a generation is definitely active.
                async for line in streaming.aiter_lines():
                    if line.startswith("data: ") and '"content"' in line:
                        break
                # A concurrent request must be rejected as busy.
                second = await client.post(
                    f"{base}/v1/chat/completions",
                    json={"model": MODEL_ID, "messages": [{"role": "user", "content": "yo"}]},
                )
                assert second.status_code == 503
                assert second.json()["error"]["code"] == "model_busy"

    with running_app(app) as base:
        asyncio.run(body(base))
