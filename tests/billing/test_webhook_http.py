"""HTTP-level outbound webhooks: operator API, egress policy, lifecycle emission,
and usage-threshold emission (Block 11.2)."""

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
from engine.billing.webhooks.store import WebhookStore
from engine.config import Settings
from engine.main import create_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"
SECRET = "whsec_test"
ALLOWED_HOST = "hooks.example.com"
LOOPBACK = ("127.0.0.1", 40000)  # operator endpoints allow loopback dev clients


def _app(tmp_path: Path, **overrides: object) -> tuple[object, Settings]:
    settings = Settings(  # type: ignore[arg-type]
        data_dir=tmp_path,
        require_auth=True,
        billing_provider="stripe",
        stripe_webhook_secret=SECRET,
        webhooks_enabled=True,
        egress_allowlist=[ALLOWED_HOST],
        **overrides,
    )
    fake = FakeBackend(tokens=["a", "b"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def _stripe_sig(payload: bytes) -> str:
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _post_stripe(client: TestClient, event: dict) -> object:
    raw = json.dumps(event).encode()
    return client.post(
        "/billing/webhooks/stripe", content=raw, headers={"stripe-signature": _stripe_sig(raw)}
    )


# ---- operator API + egress policy -----------------------------------------


def test_create_endpoint_requires_allowlisted_host(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        BillingStore(settings.db_path).create_client(id="c1")  # type: ignore[arg-type]
        # Host not in egress_allowlist -> rejected.
        bad = client.post(
            "/admin/billing/webhooks/endpoints",
            json={"client_id": "c1", "url": "https://evil.example.net/hook"},
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "egress_not_allowed"
        # Allowlisted host -> created, secret returned once.
        ok = client.post(
            "/admin/billing/webhooks/endpoints",
            json={"client_id": "c1", "url": f"https://{ALLOWED_HOST}/hook"},
        )
        assert ok.status_code == 200
        assert ok.json()["secret"].startswith("whsec_")


def test_create_endpoint_unknown_client(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        resp = client.post(
            "/admin/billing/webhooks/endpoints",
            json={"client_id": "nope", "url": f"https://{ALLOWED_HOST}/h"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "client_not_found"


def test_rotate_and_replay_and_list(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        wh = WebhookStore(settings.db_path)  # type: ignore[arg-type]
        BillingStore(settings.db_path).create_client(id="c1")  # type: ignore[arg-type]
        ep, first = wh.create_endpoint(client_id="c1", url=f"https://{ALLOWED_HOST}/h")
        # rotate-secret returns a new secret.
        rot = client.post(f"/admin/billing/webhooks/endpoints/{ep.id}/rotate-secret")
        assert rot.status_code == 200 and rot.json()["secret"] != first
        # A dead delivery can be replayed.
        wh.enqueue_event(client_id="c1", event_id="e1", event_type="x", payload="{}")
        d = wh.list_deliveries()[0]
        wh.mark_dead(d.id, status_code=500, error="x")
        replay = client.post(f"/admin/billing/webhooks/deliveries/{d.id}/replay")
        assert replay.status_code == 200
        listed = client.get("/admin/billing/webhooks/deliveries", params={"status": "pending"})
        assert any(x["id"] == d.id for x in listed.json()["deliveries"])


# ---- lifecycle emission ----------------------------------------------------


def test_stripe_cancellation_emits_outbound_canceled(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        wh = WebhookStore(settings.db_path)  # type: ignore[arg-type]
        billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
        billing.create_client(id="c1", external_ref="cus_1")
        wh.create_endpoint(client_id="c1", url=f"https://{ALLOWED_HOST}/h")
        resp = _post_stripe(
            client,
            {
                "id": "evt_del",
                "type": "customer.subscription.deleted",
                "data": {"object": {"id": "sub_1", "customer": "cus_1"}},
            },
        )
        assert resp.status_code == 200 and resp.json()["action"] == "canceled"
        deliveries = wh.list_deliveries()
        assert len(deliveries) == 1
        assert deliveries[0].event_type == "subscription.canceled"
        # Re-delivering the same inbound event does not fan out a duplicate.
        # (inbound idempotency already blocks it; the outbound id is deterministic.)


# ---- usage-threshold emission ---------------------------------------------


def test_usage_threshold_emits_once(tmp_path: Path) -> None:
    app, settings = _app(tmp_path, webhook_usage_threshold_pct=0.001)
    with TestClient(app, client=LOOPBACK) as client:
        wh = WebhookStore(settings.db_path)  # type: ignore[arg-type]
        billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
        keys = KeyStore(settings.db_path)  # type: ignore[arg-type]
        # Weekly limit 1000 admits the request; threshold 0.1% = 1 CU is crossed.
        billing.upsert_plan(id="pro", name="Pro", quota_5h_cu=1000, quota_weekly_cu=1000)
        c = billing.create_client(id="c1", external_ref="cus_1")
        record, token = keys.create()
        billing.attach_key(record.id, c.id)
        billing.upsert_subscription(
            id="s1",
            client_id=c.id,
            plan_id="pro",
            provider="stripe",
            provider_sub_id="sub_1",
            status="active",
        )
        wh.create_endpoint(client_id="c1", url=f"https://{ALLOWED_HOST}/h")

        # A served request records usage; the tiny threshold is crossed.
        body = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
        }
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post("/v1/chat/completions", json=body, headers=headers).status_code == 200
        assert client.post("/v1/chat/completions", json=body, headers=headers).status_code == 200

        usage_deliveries = [
            d for d in wh.list_deliveries() if d.event_type == "usage.threshold.reached"
        ]
        # Deterministic per-week id -> exactly one delivery despite two requests.
        assert len(usage_deliveries) == 1


def test_webhooks_disabled_no_worker_no_emit(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[arg-type]
        data_dir=tmp_path,
        require_auth=True,
        billing_provider="stripe",
        stripe_webhook_secret=SECRET,  # webhooks_enabled defaults False
    )
    fake = FakeBackend(tokens=["a"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(settings, backend=fake)
    with TestClient(app, client=LOOPBACK) as client:
        billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
        billing.create_client(id="c1", external_ref="cus_1")
        WebhookStore(settings.db_path).create_endpoint(  # type: ignore[arg-type]
            client_id="c1", url=f"https://{ALLOWED_HOST}/h"
        )
        _post_stripe(
            client,
            {
                "id": "e1",
                "type": "customer.subscription.deleted",
                "data": {"object": {"id": "s1", "customer": "cus_1"}},
            },
        )
        # Dispatcher disabled -> no outbound deliveries enqueued.
        assert WebhookStore(settings.db_path).list_deliveries() == []  # type: ignore[arg-type]
