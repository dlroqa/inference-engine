"""Error contract: unsupported params fail clearly; correct statuses/shapes."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend


def _assert_openai_error(resp, status: int) -> dict:  # type: ignore[no-untyped-def]
    assert resp.status_code == status
    body = resp.json()
    assert "error" in body
    err = body["error"]
    assert set(err) >= {"message", "type", "param", "code"}
    return err


def test_unknown_and_benign_params_are_accepted(client: TestClient) -> None:
    # Drop-in compatibility: unknown/benign optional params (reasoning_effort,
    # frequency_penalty, user, stream_options, an extra message field) are ignored,
    # not rejected, so real OpenAI-compatible clients work unchanged.
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi", "name": "bob"}],
            "reasoning_effort": "low",
            "frequency_penalty": 0.5,
            "presence_penalty": 0.2,
            "user": "u123",
            "stream_options": {"include_usage": True},
            "logit_bias": {"123": -100},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["object"] == "chat.completion"


def test_response_format_text_is_accepted(client: TestClient) -> None:
    # The OpenAI default response_format is {"type": "text"} — we produce text.
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "text"},
        },
    )
    assert resp.status_code == 200


def test_tools_are_rejected_clearly(client: TestClient) -> None:
    # Silently ignoring tools would mislead a caller expecting tool calls.
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
        },
    )
    err = _assert_openai_error(resp, 400)
    assert err["type"] == "invalid_request_error"


def test_json_response_format_rejected_when_backend_lacks_support(client: TestClient) -> None:
    # The default fake backend does not support structured output, so a JSON
    # response_format is rejected clearly (never silently unconstrained text).
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
    )
    _assert_openai_error(resp, 400)
    assert resp.json()["error"]["code"] == "structured_output_unsupported"


def test_unknown_response_format_type_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "yaml"},
        },
    )
    _assert_openai_error(resp, 400)


def test_n_greater_than_one_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}], "n": 2},
    )
    _assert_openai_error(resp, 400)


def test_unsupported_role_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "tool", "content": "x"}]},
    )
    _assert_openai_error(resp, 400)


def test_empty_messages_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": []},
    )
    _assert_openai_error(resp, 400)


def test_model_mismatch_404(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    err = _assert_openai_error(resp, 404)
    assert err["code"] == "model_not_found"
    assert err["param"] == "model"


def test_no_model_loaded_503(client_no_model: TestClient) -> None:
    resp = client_no_model.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    err = _assert_openai_error(resp, 503)
    assert err["code"] == "model_not_loaded"


def test_generation_failure_500(tmp_settings: Settings) -> None:
    fake = FakeBackend(tokens=["a", "b"], fail_after=0, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(tmp_settings, backend=fake)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
        )
        err = _assert_openai_error(resp, 500)
        assert err["type"] == "server_error"
