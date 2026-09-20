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


def test_unsupported_parameter_rejected(client: TestClient) -> None:
    # frequency_penalty is not in the supported subset -> extra=forbid rejects it.
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "frequency_penalty": 0.5,
        },
    )
    err = _assert_openai_error(resp, 400)
    assert err["type"] == "invalid_request_error"


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
