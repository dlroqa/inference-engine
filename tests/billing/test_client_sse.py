"""HTTP-level client SSE: poll/reconciliation, Last-Event-ID recovery, CORS,
isolation, and unified emission (Block 11.5)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from engine.api.client_router import sse_event_stream
from engine.auth.keys import KeyStore
from engine.billing.client_events import ClientEventLog, ClientEventNotifier
from engine.billing.store import BillingStore
from engine.config import Settings
from engine.main import create_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"
SECRET = "whsec_test"
ORIGIN = "https://app.example.com"


def _app(tmp_path: Path, *, enabled: bool = True, **overrides: object) -> tuple[object, Settings]:
    settings = Settings(  # type: ignore[arg-type]
        data_dir=tmp_path,
        require_auth=True,
        client_events_enabled=enabled,
        client_sse_heartbeat_s=1.0,
        client_cors_origins=[ORIGIN],
        billing_provider="stripe",
        stripe_webhook_secret=SECRET,
        **overrides,
    )
    fake = FakeBackend(tokens=["a", "b"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def _client_key(settings: Settings, client_id: str) -> str:
    billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
    keys = KeyStore(settings.db_path)  # type: ignore[arg-type]
    billing.create_client(id=client_id, external_ref=f"cus_{client_id}")
    record, token = keys.create(label=client_id)
    billing.attach_key(record.id, client_id)
    return token


def _seed(settings: Settings, client_id: str, *event_ids: str) -> None:
    log = ClientEventLog(settings.db_path)  # type: ignore[arg-type]
    for eid in event_ids:
        log.append(
            client_id=client_id,
            event_id=eid,
            event_type="subscription.updated",
            payload=json.dumps({"id": eid, "type": "subscription.updated", "data": {"e": eid}}),
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- poll / reconciliation -------------------------------------------------


def test_poll_returns_tail_and_last_id(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _client_key(settings, "c1")
        _seed(settings, "c1", "a", "b", "c")
        page = client.get("/client/events", headers=_auth(token)).json()
        assert [e["event_id"] for e in page["events"]] == ["a", "b", "c"]
        last = page["last_id"]
        # Resume from last_id -> nothing new.
        page2 = client.get("/client/events", params={"since": last}, headers=_auth(token)).json()
        assert page2["events"] == [] and page2["last_id"] == last


def test_poll_isolation(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token_a = _client_key(settings, "a")
        _client_key(settings, "b")
        _seed(settings, "a", "a1")
        _seed(settings, "b", "b1")
        page = client.get("/client/events", headers=_auth(token_a)).json()
        assert [e["event_id"] for e in page["events"]] == ["a1"]  # never b's


def test_disabled_returns_404(tmp_path: Path) -> None:
    app, settings = _app(tmp_path, enabled=False)
    with TestClient(app) as client:
        token = _client_key(settings, "c1")
        assert client.get("/client/events", headers=_auth(token)).status_code == 404
        r = client.get("/client/events/stream", headers=_auth(token))
        assert r.status_code == 404


# ---- SSE stream ------------------------------------------------------------


def _drain(gen) -> list[bytes]:
    async def run() -> list[bytes]:
        out: list[bytes] = []
        async for chunk in gen:
            out.append(chunk)
        return out

    return asyncio.run(run())


def test_sse_stream_replays_from_cursor_and_frames(tmp_path: Path) -> None:
    # The stream generator is tested directly with an injected is_disconnected,
    # because a live server cannot reliably signal disconnect through TestClient.
    app, settings = _app(tmp_path)
    with TestClient(app):  # runs migrations / seeds default plan
        _client_key(settings, "c1")
    _seed(settings, "c1", "a", "b", "c")
    log = ClientEventLog(settings.db_path)  # type: ignore[arg-type]
    notifier = ClientEventNotifier()

    async def _disconnected_after_replay() -> bool:
        return True  # stop once the initial replay is drained

    gen = sse_event_stream(
        log=log,
        notifier=notifier,
        client_id="c1",
        cursor=1,
        heartbeat_s=1.0,
        is_disconnected=_disconnected_after_replay,
    )
    chunks = _drain(gen)
    text = b"".join(chunks).decode()
    # Resume after id 1 -> only b and c, each a well-formed SSE frame.
    assert text.startswith(": connected\n\n")
    assert "id: 2\nevent: subscription.updated\ndata: " in text
    assert "id: 3\nevent: subscription.updated\ndata: " in text
    assert '"e": "b"' in text and '"e": "c"' in text
    assert '"e": "a"' not in text  # id 1 was before the cursor


def test_sse_stream_heartbeat_when_idle(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app):
        _client_key(settings, "c1")
    log = ClientEventLog(settings.db_path)  # type: ignore[arg-type]
    notifier = ClientEventNotifier()
    calls = {"n": 0}

    async def _disconnected_second_pass() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # allow one idle wait (-> heartbeat), then stop

    gen = sse_event_stream(
        log=log,
        notifier=notifier,
        client_id="c1",
        cursor=0,
        heartbeat_s=0.05,
        is_disconnected=_disconnected_second_pass,
    )
    text = b"".join(_drain(gen)).decode()
    assert ": keep-alive\n\n" in text


def test_sse_requires_client_key(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        # No key at all.
        assert client.get("/client/events/stream").status_code == 401
        # Operator/unowned key -> not a client credential.
        _, op = KeyStore(settings.db_path).create(label="op")  # type: ignore[arg-type]
        assert client.get("/client/events/stream", params={"api_key": op}).status_code == 403


# ---- unified emission (webhook lifecycle also lands in the client log) ------


def test_stripe_cancellation_lands_in_client_log(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _client_key(settings, "c1")  # external_ref cus_c1; revoked on cancel
        raw = json.dumps(
            {
                "id": "evt_del",
                "type": "customer.subscription.deleted",
                "data": {"object": {"id": "sub_1", "customer": "cus_c1"}},
            }
        ).encode()
        ts = int(time.time())
        sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        resp = client.post(
            "/billing/webhooks/stripe",
            content=raw,
            headers={"stripe-signature": f"t={ts},v1={sig}"},
        )
        assert resp.status_code == 200 and resp.json()["action"] == "canceled"
        # Cancellation revokes the client's keys (11.1), so the client can no longer
        # poll — but the unified emitter still recorded the event in the client log.
        assert client.get("/client/events", headers=_auth(token)).status_code == 401
        log = ClientEventLog(settings.db_path)  # type: ignore[arg-type]
        assert [e.type for e in log.list_since("c1", 0)] == ["subscription.canceled"]


# ---- CORS (scoped to /client/*) --------------------------------------------


def test_cors_allowed_origin_on_client_routes(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _client_key(settings, "c1")
        resp = client.get("/client/me", headers={**_auth(token), "origin": ORIGIN})
        assert resp.headers.get("access-control-allow-origin") == ORIGIN
        # Preflight is answered.
        pre = client.options("/client/events", headers={"origin": ORIGIN})
        assert pre.status_code == 204
        assert pre.headers.get("access-control-allow-origin") == ORIGIN


def test_cors_disallowed_origin_and_scope(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _client_key(settings, "c1")
        # A disallowed origin gets no CORS header.
        bad = client.get("/client/me", headers={**_auth(token), "origin": "https://evil.test"})
        assert "access-control-allow-origin" not in bad.headers
        # A non-/client route is unaffected even for an allowed origin.
        health = client.get("/healthz", headers={"origin": ORIGIN})
        assert "access-control-allow-origin" not in health.headers
