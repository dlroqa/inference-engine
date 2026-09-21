"""Per-key concurrency limit rejects a second in-flight request."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx

from engine.auth.keys import KeyStore
from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import running_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"


def test_concurrency_limit_returns_429(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, require_auth=True, max_concurrent_per_key=1)
    fake = FakeBackend(tokens=["x"] * 1000, per_token_delay=0.02, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(settings, backend=fake)

    # Migrate + mint a key before the server thread starts serving.
    from engine.store.db import connect
    from engine.store.migrations import apply_migrations

    conn = connect(settings.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    _, token = KeyStore(settings.db_path).create()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]}

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            started = asyncio.Event()

            async def hold_first() -> None:
                # Keep genuinely consuming the stream so the first request stays
                # in-flight (and holds its per-key slot) for the duration.
                async with client.stream(
                    "POST",
                    f"{base}/v1/chat/completions",
                    headers=headers,
                    json={**payload, "stream": True},
                ) as streaming:
                    assert streaming.status_code == 200
                    async for line in streaming.aiter_lines():
                        if line.startswith("data: ") and '"content"' in line:
                            started.set()

            task = asyncio.create_task(hold_first())
            try:
                await started.wait()  # first request is generating
                # A second request for the same key exceeds max_concurrent_per_key.
                second = await client.post(
                    f"{base}/v1/chat/completions", headers=headers, json=payload
                )
                assert second.status_code == 429
                assert second.json()["error"]["code"] == "concurrency_limit_exceeded"
            finally:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    with running_app(app) as base:
        asyncio.run(body(base))
