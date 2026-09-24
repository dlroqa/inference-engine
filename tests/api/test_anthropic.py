"""Anthropic Messages API (Block 8) edge tests.

Non-streaming shape, streaming event ordering, sampler translation, and honest
rejection of unsupported features. Client-disconnect and saturation paths use a
real Uvicorn server (in-process ASGI cannot disconnect mid-stream).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID, running_app
from tests.support.fake_backend import FakeBackend

TOKENS = ["Hello", ", ", "world", "!"]
TEXT = "".join(TOKENS)


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[TestClient]:
    fake = FakeBackend(tokens=TOKENS, model_id=MODEL_ID)
    asyncio.run(fake.load())
    with TestClient(create_app(tmp_settings, backend=fake)) as c:
        yield c


def _msg(**kw: object) -> dict:
    body = {"model": MODEL_ID, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    body.update(kw)
    return body


def test_messages_non_streaming_shape(client: TestClient) -> None:
    resp = client.post("/v1/messages", json=_msg())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == MODEL_ID
    assert body["content"] == [{"type": "text", "text": TEXT}]
    assert body["stop_reason"] == "end_turn"
    assert body["stop_sequence"] is None
    assert body["usage"]["output_tokens"] == len(TOKENS)
    assert body["id"].startswith("msg_")
    assert resp.headers["x-request-id"].startswith("msg_")


def test_system_and_block_content_accepted(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_msg(
            system=[{"type": "text", "text": "be terse"}],
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"][0]["text"] == TEXT


def test_sampler_translation(client: TestClient) -> None:
    # The request is accepted with Anthropic sampling params mapped to the backend.
    resp = client.post(
        "/v1/messages",
        json=_msg(temperature=0.3, top_p=0.5, top_k=10, stop_sequences=["STOP"]),
    )
    assert resp.status_code == 200, resp.text


def test_streaming_event_sequence(client: TestClient) -> None:
    events: list[str] = []
    payloads: list[dict] = []
    with client.stream("POST", "/v1/messages", json=_msg(stream=True)) as s:
        assert s.status_code == 200
        assert s.headers["content-type"].startswith("text/event-stream")
        cur_event = None
        for line in s.iter_lines():
            if line.startswith("event: "):
                cur_event = line[len("event: ") :]
                events.append(cur_event)
            elif line.startswith("data: "):
                payloads.append(json.loads(line[len("data: ") :]))

    # Required ordering (a ping may appear after content_block_start).
    assert events[0] == "message_start"
    assert events[1] == "content_block_start"
    assert "content_block_delta" in events
    assert events[-3:] == ["content_block_stop", "message_delta", "message_stop"]

    # message_start carries input-token usage; message_delta the stop reason + output tokens.
    start = next(p for p in payloads if p["type"] == "message_start")
    assert start["message"]["usage"]["input_tokens"] >= 0
    deltas = [p for p in payloads if p["type"] == "content_block_delta"]
    assert "".join(d["delta"]["text"] for d in deltas) == TEXT
    md = next(p for p in payloads if p["type"] == "message_delta")
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["output_tokens"] == len(TOKENS)


def test_tools_rejected(client: TestClient) -> None:
    resp = client.post("/v1/messages", json=_msg(tools=[{"name": "x"}]))
    assert resp.status_code == 400
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


def test_tool_choice_rejected(client: TestClient) -> None:
    resp = client.post("/v1/messages", json=_msg(tool_choice={"type": "auto"}))
    assert resp.status_code == 400
    assert resp.json()["type"] == "error"


def test_non_text_content_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_msg(messages=[{"role": "user", "content": [{"type": "image", "source": {}}]}]),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_model_not_found(client: TestClient) -> None:
    resp = client.post("/v1/messages", json=_msg(model="not-the-model"))
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found_error"


def test_no_model_loaded(tmp_settings: Settings) -> None:
    # Configured for MODEL_ID but unloaded: known model, no ready backend -> 503
    # (Block 12.1). A truly unknown model would be 404.
    settings = tmp_settings.model_copy(update={"model_id": MODEL_ID})
    with TestClient(create_app(settings)) as client:
        resp = client.post("/v1/messages", json=_msg())
    assert resp.status_code == 503
    assert resp.json()["error"]["type"] == "api_error"


def test_saturation_returns_overloaded(tmp_path: Path) -> None:
    fake = FakeBackend(tokens=["x"] * 40, per_token_delay=0.01, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(Settings(data_dir=tmp_path, max_concurrency_queue=0), backend=fake)

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:

            async def hold() -> None:
                async with client.stream(
                    "POST", f"{base}/v1/messages", json=_msg(stream=True)
                ) as s:
                    async for _ in s.aiter_lines():
                        pass

            task = asyncio.create_task(hold())
            try:
                await asyncio.sleep(0.05)
                second = await client.post(f"{base}/v1/messages", json=_msg())
                assert second.status_code == 529
                assert second.json()["error"]["type"] == "overloaded_error"
                assert second.headers.get("retry-after") is not None
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    with running_app(app) as base:
        asyncio.run(body(base))


def test_disconnect_frees_capacity(tmp_path: Path) -> None:
    fake = FakeBackend(tokens=["x"] * 200, per_token_delay=0.01, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(Settings(data_dir=tmp_path), backend=fake)

    async def body(base: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", f"{base}/v1/messages", json=_msg(stream=True)) as s:
                async for line in s.aiter_lines():
                    if '"content_block_delta"' in line:
                        break
                # Leaving the context disconnects mid-generation.
            for _ in range(100):
                sched = (await client.get(f"{base}/metrics")).json()["scheduler"]
                if sched["in_use"] == 0:
                    break
                await asyncio.sleep(0.02)
            assert sched["in_use"] == 0
            assert sched["cancelled_total"] >= 1

    with running_app(app) as base:
        asyncio.run(body(base))


def test_generation_failure_non_stream(tmp_settings: Settings) -> None:
    fake = FakeBackend(tokens=["a", "b", "c"], fail_after=1, model_id=MODEL_ID)
    asyncio.run(fake.load())
    with TestClient(create_app(tmp_settings, backend=fake)) as client:
        resp = client.post("/v1/messages", json=_msg())
    assert resp.status_code == 500
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"


def test_generation_failure_mid_stream(client_failing: TestClient) -> None:
    events: list[str] = []
    with client_failing.stream("POST", "/v1/messages", json=_msg(stream=True)) as s:
        assert s.status_code == 200  # headers already sent before the failure
        for line in s.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: ") :])
    # A mid-stream failure surfaces a terminal error event after some deltas.
    assert "error" in events


@pytest.fixture
def client_failing(tmp_settings: Settings) -> Iterator[TestClient]:
    fake = FakeBackend(tokens=["a", "b", "c", "d"], fail_after=2, model_id=MODEL_ID)
    asyncio.run(fake.load())
    with TestClient(create_app(tmp_settings, backend=fake)) as c:
        yield c
