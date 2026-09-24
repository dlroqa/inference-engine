"""Fixtures for OpenAI-edge API tests.

A ready :class:`FakeBackend` is injected into the app so the HTTP surface is
tested without llama.cpp or a real model.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"
TOKENS = ["Hello", ", ", "world", "!"]
EXPECTED_TEXT = "".join(TOKENS)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@contextlib.contextmanager
def running_app(app: FastAPI) -> Iterator[str]:
    """Run ``app`` on a real Uvicorn server in a thread; yield its base URL.

    A real server is required to exercise client-disconnect cancellation and the
    OpenAI SDK, which the in-process ASGI transport cannot (it buffers streams).
    """
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _loaded_fake(**kwargs: object) -> FakeBackend:
    fake = FakeBackend(tokens=TOKENS, model_id=MODEL_ID, **kwargs)  # type: ignore[arg-type]
    asyncio.run(fake.load())
    return fake


@pytest.fixture
def fake_backend() -> FakeBackend:
    return _loaded_fake()


@pytest.fixture
def client(tmp_settings: Settings, fake_backend: FakeBackend) -> Iterator[TestClient]:
    app = create_app(tmp_settings, backend=fake_backend)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_no_model(tmp_settings: Settings) -> Iterator[TestClient]:
    # Configured to serve MODEL_ID but nothing loaded: the model stays *known*
    # (Block 12.1) so a request for it is 503 (model_not_loaded), not 404.
    app = create_app(tmp_settings.model_copy(update={"model_id": MODEL_ID}))
    with TestClient(app) as c:
        yield c
