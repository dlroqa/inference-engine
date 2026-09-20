"""POST /v1/chat/completions — non-streaming and streaming (SSE framing)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.api.conftest import EXPECTED_TEXT, MODEL_ID


def _parse_sse(text: str) -> list[str]:
    """Return the raw payloads of each `data:` line, in order."""
    payloads = []
    for line in text.splitlines():
        if line.startswith("data: "):
            payloads.append(line[len("data: ") :])
    return payloads


def test_non_streaming(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi there"}],
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-request-id", "").startswith("chatcmpl-")
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == MODEL_ID
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == EXPECTED_TEXT
    assert choice["finish_reason"] == "stop"
    usage = body["usage"]
    assert usage["completion_tokens"] == 4
    assert usage["prompt_tokens"] == 2  # "hi there"
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_streaming_framing(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    payloads = _parse_sse(resp.text)
    assert payloads[-1] == "[DONE]"

    events = [json.loads(p) for p in payloads[:-1]]
    # Opening chunk carries the assistant role.
    assert events[0]["choices"][0]["delta"].get("role") == "assistant"
    assert events[0]["object"] == "chat.completion.chunk"

    # Content deltas reconstruct the full message.
    content = "".join(e["choices"][0]["delta"].get("content") or "" for e in events)
    assert content == EXPECTED_TEXT

    # Final chunk carries the finish reason.
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def test_max_tokens_and_stop_accepted(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "temperature": 0.0,
            "top_p": 0.9,
            "stop": ["\n\n"],
            "seed": 123,
        },
    )
    assert resp.status_code == 200
