"""HTTP-level auth, limits, quota, and attribution (Block 3)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from engine.auth.keys import KeyStore
from engine.config import Settings
from engine.main import create_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"


def _app(tmp_path: Path, **overrides: object) -> tuple[object, Settings]:
    settings = Settings(data_dir=tmp_path, require_auth=True, **overrides)  # type: ignore[arg-type]
    fake = FakeBackend(tokens=["a", "b"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def _new_key(settings: Settings, label: str = "t") -> str:
    _, token = KeyStore(settings.db_path).create(label=label)  # type: ignore[arg-type]
    return token


def _chat(client: TestClient, token: str | None = None, **body: object) -> object:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}], **body}
    return client.post("/v1/chat/completions", json=payload, headers=headers)


def test_missing_key_rejected(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        resp = _chat(client)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "missing_api_key"


def test_invalid_key_rejected(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        resp = _chat(client, token="sk-ie-not-real")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"


def test_revoked_key_rejected(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        store = KeyStore(settings.db_path)  # type: ignore[arg-type]
        record, token = store.create()
        store.revoke(record.id)
        resp = _chat(client, token=token)
        assert resp.status_code == 401


def test_valid_key_works_and_sets_quota_headers(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _new_key(settings)
        resp = _chat(client, token=token)
        assert resp.status_code == 200
        assert "x-ratelimit-remaining-cu-5h" in resp.headers
        assert "x-ratelimit-remaining-cu-week" in resp.headers


def test_rate_limit(tmp_path: Path) -> None:
    app, settings = _app(tmp_path, rate_limit_per_min=1)
    with TestClient(app) as client:
        token = _new_key(settings)
        assert _chat(client, token=token).status_code == 200
        resp = _chat(client, token=token)
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "rate_limit_exceeded"
        assert resp.headers.get("retry-after") == "60"


def test_5h_quota_blocks_then_headers(tmp_path: Path) -> None:
    # First request costs CU=1(prompt)+2(completion)=3; limit 5 blocks the second.
    app, settings = _app(tmp_path, quota_5h_cu=5.0, quota_weekly_cu=0.0)
    with TestClient(app) as client:
        token = _new_key(settings)
        assert _chat(client, token=token, max_tokens=2).status_code == 200
        resp = _chat(client, token=token, max_tokens=2)
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "quota_exceeded"
        assert "retry-after" in resp.headers


def test_weekly_quota_blocks_independently(tmp_path: Path) -> None:
    # 5h unlimited, weekly cap small -> weekly blocks even with 5h headroom.
    app, settings = _app(tmp_path, quota_5h_cu=0.0, quota_weekly_cu=5.0)
    with TestClient(app) as client:
        token = _new_key(settings)
        assert _chat(client, token=token, max_tokens=2).status_code == 200
        resp = _chat(client, token=token, max_tokens=2)
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "quota_exceeded"


def test_long_request_debits_more_cu(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _new_key(settings)
        _chat(client, token=token, messages=[{"role": "user", "content": "a b c d e f"}])
        conn = sqlite3.connect(str(settings.db_path))
        cu_long = conn.execute("SELECT cu FROM usage_events ORDER BY id DESC LIMIT 1;").fetchone()[
            0
        ]
        conn.close()
        # prompt tokens = 6 words + 2 completion tokens = 8 CU (> a 1-word prompt).
        assert cu_long == 8.0


def test_body_too_large_413(tmp_path: Path) -> None:
    app, settings = _app(tmp_path, max_request_bytes=10)
    with TestClient(app) as client:
        token = _new_key(settings)
        resp = _chat(client, token=token)
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"


def test_attribution_recorded_without_secrets(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _new_key(settings)
        resp = _chat(client, token=token)
        rid = resp.headers["x-request-id"]
        conn = sqlite3.connect(str(settings.db_path))
        row = conn.execute(
            "SELECT key_id, request_id, endpoint, prompt_tokens, completion_tokens "
            "FROM usage_events WHERE request_id = ?;",
            (rid,),
        ).fetchone()
        # The token/secret is never written anywhere in usage_events.
        dump = conn.execute(
            "SELECT group_concat(request_id || key_id) FROM usage_events;"
        ).fetchone()[0]
        conn.close()
        assert row is not None
        assert row[1] == rid
        assert row[2] == "/v1/chat/completions"
        assert token not in (dump or "")


def test_loopback_allows_anonymous(tmp_path: Path) -> None:
    # Default (loopback, require_auth unset) -> no key needed, attributed as local.
    settings = Settings(data_dir=tmp_path)
    fake = FakeBackend(tokens=["a", "b"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(settings, backend=fake)
    with TestClient(app) as client:
        assert _chat(client).status_code == 200
        conn = sqlite3.connect(str(settings.db_path))
        key_id = conn.execute("SELECT key_id FROM usage_events LIMIT 1;").fetchone()[0]
        conn.close()
        assert key_id == "local"
