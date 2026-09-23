"""HTTP-level billing: webhook signature/idempotency and entitlement enforcement
at the gateway (Block 11.1)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from engine.auth.keys import KeyStore
from engine.billing.store import BillingStore
from engine.config import Settings
from engine.main import create_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"
SECRET = "whsec_test"


def _app(tmp_path: Path, **overrides: object) -> tuple[object, Settings]:
    settings = Settings(  # type: ignore[arg-type]
        data_dir=tmp_path,
        require_auth=True,
        billing_provider="stripe",
        stripe_webhook_secret=SECRET,
        **overrides,
    )
    fake = FakeBackend(tokens=["a", "b"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def _sign(payload: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _chat(client: TestClient, token: str, **body: object) -> object:
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        **body,
    }
    return client.post(
        "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


def _post_webhook(client: TestClient, event: dict, *, sign: bool = True) -> object:
    raw = json.dumps(event).encode()
    headers = {"stripe-signature": _sign(raw)} if sign else {"stripe-signature": "t=1,v1=bad"}
    return client.post("/billing/webhooks/stripe", content=raw, headers=headers)


# ---- webhook endpoint ------------------------------------------------------


def test_webhook_valid_signature_processed(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        BillingStore(settings.db_path).upsert_plan(  # type: ignore[arg-type]
            id="pro", name="Pro", quota_5h_cu=10.0, quota_weekly_cu=100.0
        )
        event = {
            "id": "evt_1",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "active",
                    "metadata": {"plan": "pro"},
                }
            },
        }
        resp = _post_webhook(client, event)
        assert resp.status_code == 200
        assert resp.json()["action"] == "activated"


def test_webhook_invalid_signature_rejected_no_state_change(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        event = {
            "id": "evt_1",
            "type": "invoice.payment_failed",
            "data": {"object": {"customer": "cus_1"}},
        }
        resp = _post_webhook(client, event, sign=False)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_signature"
        # No event was recorded, so a subsequent valid delivery is still "new".
        billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
        assert billing.record_event_once("stripe", "evt_1", "x") is True


def test_webhook_duplicate_is_idempotent(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        BillingStore(settings.db_path).create_client(  # type: ignore[arg-type]
            id="c1", external_ref="cus_1"
        )
        event = {
            "id": "evt_dup",
            "type": "invoice.payment_failed",
            "data": {"object": {"customer": "cus_1"}},
        }
        first = _post_webhook(client, event)
        second = _post_webhook(client, event)
        assert first.status_code == 200 and first.json()["action"] == "suspended"
        assert second.status_code == 200 and second.json()["status"] == "duplicate"


def test_webhook_disabled_returns_404(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, require_auth=True)  # type: ignore[arg-type]
    fake = FakeBackend(tokens=["a"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(settings, backend=fake)
    with TestClient(app) as client:
        resp = client.post(
            "/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "t=1,v1=x"}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "billing_disabled"


# ---- entitlement enforcement ----------------------------------------------


def _owned_key(
    settings: Settings,
    *,
    plan_id: str | None,
    client_status: str = "active",
    key_status: str = "active",
    **plan_kwargs: object,
) -> str:
    billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
    keys = KeyStore(settings.db_path)  # type: ignore[arg-type]
    client = billing.create_client(id="c1", external_ref="cus_1")
    record, token = keys.create()
    billing.attach_key(record.id, client.id)
    if plan_id is not None:
        billing.upsert_plan(id=plan_id, name=plan_id, **plan_kwargs)  # type: ignore[arg-type]
        billing.upsert_subscription(
            id="s1",
            client_id=client.id,
            plan_id=plan_id,
            provider="stripe",
            provider_sub_id="sub_1",
            status="active",
        )
    if client_status != "active":
        billing.set_client_status(client.id, client_status)
    if key_status == "revoked":
        billing.revoke_client_keys(client.id)
    return token


def test_active_plan_key_serves(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _owned_key(
            settings, plan_id="pro", quota_5h_cu=1_000_000.0, quota_weekly_cu=5_000_000.0
        )
        assert _chat(client, token).status_code == 200


def test_suspended_client_key_refused(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _owned_key(
            settings,
            plan_id="pro",
            client_status="suspended",
            quota_5h_cu=1_000_000.0,
            quota_weekly_cu=5_000_000.0,
        )
        resp = _chat(client, token)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "key_suspended"


def test_revoked_key_refused(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _owned_key(
            settings,
            plan_id="pro",
            key_status="revoked",
            quota_5h_cu=1_000_000.0,
            quota_weekly_cu=5_000_000.0,
        )
        # revoked_at is set, so the auth layer rejects it first (401).
        resp = _chat(client, token)
        assert resp.status_code == 401


def test_model_not_in_plan_refused(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _owned_key(
            settings,
            plan_id="pro",
            quota_5h_cu=1_000_000.0,
            quota_weekly_cu=5_000_000.0,
            allowed_models=["some-other-model"],
        )
        resp = _chat(client, token)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "model_not_entitled"


def test_plan_quota_applied(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        # A tiny plan quota is exceeded by the request estimate -> 429.
        token = _owned_key(settings, plan_id="pro", quota_5h_cu=1.0, quota_weekly_cu=1.0)
        resp = _chat(client, token)
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "quota_exceeded"


def test_unowned_key_unaffected_by_billing(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        _, token = KeyStore(settings.db_path).create()  # type: ignore[arg-type]
        assert _chat(client, token).status_code == 200
