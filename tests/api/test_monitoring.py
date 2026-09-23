"""HTTP-level monitoring aggregations: attribution, taxonomy, alerts (Block 11.6a)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from engine.auth.keys import KeyStore
from engine.billing.store import BillingStore
from engine.billing.webhooks.store import WebhookStore
from engine.config import Settings
from engine.main import create_app
from engine.quota.store import UsageStore
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"
LOOPBACK = ("127.0.0.1", 40000)


def _app(tmp_path: Path, **overrides: object) -> tuple[object, Settings]:
    settings = Settings(data_dir=tmp_path, require_auth=True, **overrides)  # type: ignore[arg-type]
    fake = FakeBackend(tokens=["a", "b"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def test_requires_operator(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app, client=("203.0.113.9", 5000)) as client:  # remote, no key
        assert client.get("/admin/usage/attribution").status_code == 401
        assert client.get("/admin/errors/taxonomy").status_code == 401
        assert client.get("/admin/alerts").status_code == 401


def test_attribution_rollup_matches_usage(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
        keys = KeyStore(settings.db_path)  # type: ignore[arg-type]
        usage = UsageStore(settings.db_path)  # type: ignore[arg-type]
        billing.create_client(id="c1", external_ref="cus_1")
        rec, _ = keys.create(label="app-key")
        billing.attach_key(rec.id, "c1")
        import time as _t

        now = _t.time()
        for i in range(3):
            usage.record(
                key_id=rec.id,
                request_id=f"r{i}",
                endpoint="/v1/chat/completions",
                model=MODEL_ID,
                prompt_tokens=10,
                completion_tokens=5,
                cu=15.0,
                status=200 if i < 2 else 500,
                ts=now - 60,
            )
        page = client.get("/admin/usage/attribution", params={"window": "5h"}).json()
        row = next(k for k in page["keys"] if k["key_id"] == rec.id)
        assert row["requests"] == 3 and row["cu"] == 45.0
        assert row["prompt_tokens"] == 30 and row["completion_tokens"] == 15
        assert row["errors"] == 1  # the status-500 row
        assert row["client_id"] == "c1" and row["key_label"] == "app-key"


def test_error_taxonomy_counts(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        # Emit categorized error log records via the request-error logger path.
        from engine.api.errors import log_request_error
        from engine.telemetry.taxonomy import ErrorCategory

        log = logging.getLogger("engine.request")
        # The LogCollector on the root logger persists >=WARNING rows to log_events.

        class _Req:
            class url:
                path = "/v1/chat/completions"

            class state:
                request_id = "rid"

        for cat in (ErrorCategory.AUTH, ErrorCategory.AUTH, ErrorCategory.LIMIT):
            log_request_error(
                _Req,
                category=cat,
                stage="edge",
                detail="x",
                status_code=401,  # type: ignore[arg-type]
            )
        page = client.get("/admin/errors/taxonomy", params={"window": "week"}).json()
        assert page["categories"].get("auth") == 2
        assert page["categories"].get("limit") == 1
        assert page["total"] == 3
        del log


def test_alerts_reflect_signals(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
        webhooks = WebhookStore(settings.db_path)  # type: ignore[arg-type]
        billing.create_client(id="c1")
        billing.set_client_status("c1", "suspended")
        # A dead-lettered delivery.
        webhooks.create_endpoint(client_id="c1", url="https://h/1")
        webhooks.enqueue_event(client_id="c1", event_id="e1", event_type="x", payload="{}")
        d = webhooks.list_deliveries()[0]
        webhooks.mark_dead(d.id, status_code=500, error="boom")

        alerts = client.get("/admin/alerts").json()["alerts"]
        kinds = {a["kind"] for a in alerts}
        assert "client_suspended" in kinds and "webhook_dead_letters" in kinds
        suspended = next(a for a in alerts if a["kind"] == "client_suspended")
        assert suspended["target_type"] == "client" and suspended["target_id"] == "c1"


def test_alerts_empty_when_healthy(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        BillingStore(settings.db_path).create_client(id="c1")  # type: ignore[arg-type]
        assert client.get("/admin/alerts").json()["alerts"] == []
