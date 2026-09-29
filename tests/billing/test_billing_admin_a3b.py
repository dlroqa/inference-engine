"""Billing and webhook administration contracts used by the A3b dashboard views.

- Every operator write is recorded in the audit log, with ids, labels and hosts
  only (never a token, a signing secret, or a full endpoint URL).
- Rotating a signing secret reports the grace period during which the previous
  secret still signs deliveries.
- Webhook endpoint hosts must be ASCII (the xn-- form for internationalized
  names), so delivery never goes through the IDNA 2003 conversion.
- Outbound delivery never follows a redirect: the egress allowlist is checked
  against the registered URL only (real HTTP servers on loopback).
"""

from __future__ import annotations

import asyncio
import http.server
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from engine.audit.log import AuditLog
from engine.billing.store import BillingStore
from engine.billing.webhooks.delivery import DeliveryWorker, UrllibTransport
from engine.billing.webhooks.store import WebhookStore
from engine.config import Settings
from engine.main import create_app
from engine.store.db import connect
from engine.store.migrations import apply_migrations
from tests.support.fake_backend import FakeBackend
from tests.support.operator import operator_client

ALLOWED = "hooks.example.com"
GRACE_S = 3600.0


def _app(tmp_path: Path) -> tuple[Any, Settings]:
    settings = Settings(  # type: ignore[arg-type]
        data_dir=tmp_path,
        require_auth=True,
        webhooks_enabled=False,  # no background worker: deliveries stay put
        egress_allowlist=[ALLOWED],
        webhook_signing_rotation_grace_s=GRACE_S,
    )
    fake = FakeBackend(tokens=["a"], model_id="fake")
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def _events(settings: Settings) -> list[Any]:
    return AuditLog(settings.db_path).list(limit=100)  # type: ignore[arg-type]


# ---- audit -------------------------------------------------------------------


def test_every_billing_and_webhook_write_is_audited_without_secrets(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with operator_client(app, settings) as client:
        plan = client.post("/admin/billing/plans", json={"id": "pro", "name": "Pro"})
        assert plan.status_code == 200
        created = client.post("/admin/billing/clients", json={"email": "a@example.com"}).json()
        cid = created["id"]
        key = client.post(f"/admin/billing/clients/{cid}/keys", json={"label": "app"}).json()
        url = f"https://{ALLOWED}/hook/receiver-token-in-path?sig=query-secret"
        ep = client.post(
            "/admin/billing/webhooks/endpoints", json={"client_id": cid, "url": url}
        ).json()
        eid = ep["id"]
        assert client.post(f"/admin/billing/webhooks/endpoints/{eid}/disable").status_code == 200
        enable = f"/admin/billing/webhooks/endpoints/{eid}/disable"
        assert client.post(enable, params={"disabled": "false"}).status_code == 200
        rotated = client.post(f"/admin/billing/webhooks/endpoints/{eid}/rotate-secret").json()
        wh = WebhookStore(settings.db_path)  # type: ignore[arg-type]
        wh.enqueue_event(client_id=cid, event_id="e1", event_type="x", payload="{}")
        delivery = wh.list_deliveries()[0]
        replay = client.post(f"/admin/billing/webhooks/deliveries/{delivery.id}/replay")
        assert replay.status_code == 200
        assert client.delete(f"/admin/billing/webhooks/endpoints/{eid}").status_code == 200

    events = _events(settings)
    got = [(e.action, e.target) for e in reversed(events)]
    assert got[-9:] == [
        ("billing.plan.upsert", "pro"),
        ("billing.client.create", cid),
        ("key.create", key["id"]),
        ("webhook.endpoint.create", eid),
        ("webhook.endpoint.disable", eid),
        ("webhook.endpoint.enable", eid),
        ("webhook.endpoint.rotate_secret", eid),
        ("webhook.delivery.replay", delivery.id),
        ("webhook.endpoint.delete", eid),
    ]
    by_action = {e.action: e for e in events}
    assert by_action["key.create"].detail == {"label": "app", "client_id": cid}
    assert by_action["webhook.endpoint.create"].detail == {"client_id": cid, "host": ALLOWED}
    assert by_action["webhook.endpoint.rotate_secret"].detail == {"grace_s": GRACE_S}
    # No token, signing secret, or URL path/query anywhere in the chain.
    text = repr([(e.action, e.target, e.detail) for e in events])
    for secret in (key["token"], ep["secret"], rotated["secret"], "receiver-token", "query-secret"):
        assert secret not in text
    assert AuditLog(settings.db_path).verify().ok  # type: ignore[arg-type]


# ---- rotation grace ------------------------------------------------------------


def test_rotate_secret_reports_the_grace_period_and_keeps_the_old_secret(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with operator_client(app, settings) as client:
        BillingStore(settings.db_path).create_client(id="c1")  # type: ignore[arg-type]
        ep = client.post(
            "/admin/billing/webhooks/endpoints",
            json={"client_id": "c1", "url": f"https://{ALLOWED}/h"},
        ).json()
        before = time.time()
        rotated = client.post(f"/admin/billing/webhooks/endpoints/{ep['id']}/rotate-secret")
        after = time.time()
    assert rotated.status_code == 200
    body = rotated.json()
    assert set(body) == {"id", "secret", "grace_s", "previous_secret_expires_at"}
    assert body["grace_s"] == GRACE_S
    assert before + GRACE_S <= body["previous_secret_expires_at"] <= after + GRACE_S
    # Both secrets sign deliveries until the grace period ends.
    active = WebhookStore(settings.db_path).active_secrets(ep["id"])  # type: ignore[arg-type]
    assert active == [body["secret"], ep["secret"]]


# ---- ASCII hosts only ------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "bücher." + ALLOWED,  # an IDN subdomain of an allowlisted host
        "ｈｏｏｋｓ.example.com",  # fullwidth letters that IDNA 2003 folds to ASCII
        "evil。" + ALLOWED,  # an ideographic full stop, which IDNA treats as a dot
    ],
)
def test_non_ascii_endpoint_hosts_are_rejected(tmp_path: Path, host: str) -> None:
    app, settings = _app(tmp_path)
    with operator_client(app, settings) as client:
        BillingStore(settings.db_path).create_client(id="c1")  # type: ignore[arg-type]
        resp = client.post(
            "/admin/billing/webhooks/endpoints",
            json={"client_id": "c1", "url": f"https://{host}/h"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "endpoint_host_not_ascii"
    assert WebhookStore(settings.db_path).list_endpoints() == []  # type: ignore[arg-type]


def test_the_punycode_form_of_an_allowlisted_subdomain_is_accepted(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with operator_client(app, settings) as client:
        BillingStore(settings.db_path).create_client(id="c1")  # type: ignore[arg-type]
        resp = client.post(
            "/admin/billing/webhooks/endpoints",
            json={"client_id": "c1", "url": f"https://xn--bcher-kva.{ALLOWED}/h"},
        )
    assert resp.status_code == 200


# ---- no redirects ----------------------------------------------------------------


class _Server:
    """A loopback HTTP server that records hits and answers with a fixed status."""

    def __init__(self, status: int, location: str | None = None) -> None:
        self.hits: list[tuple[str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.hits.append((self.command, self.path))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _answer

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_transport_does_not_follow_redirects(status: int) -> None:
    elsewhere = _Server(200)
    redirector = _Server(status, location=f"{elsewhere.url}/elsewhere")
    try:
        got = UrllibTransport().post(
            f"{redirector.url}/hook", '{"a":1}', {"webhook-signature": "v1,sig"}, timeout=5
        )
    finally:
        redirector.close()
        elsewhere.close()
    assert got == status  # reported as the endpoint's (non-2xx) status
    assert redirector.hits == [("POST", "/hook")]
    assert elsewhere.hits == []  # the redirect target was never contacted


def test_a_redirecting_endpoint_is_a_failed_delivery(tmp_path: Path) -> None:
    elsewhere = _Server(200)
    redirector = _Server(302, location=f"{elsewhere.url}/elsewhere")
    db = tmp_path / "db.sqlite"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    store = WebhookStore(db)
    try:
        BillingStore(db).create_client(id="c1")
        store.create_endpoint(client_id="c1", url=f"{redirector.url}/hook")
        store.enqueue_event(client_id="c1", event_id="e1", event_type="x", payload="{}")
        worker = DeliveryWorker(store, max_attempts=1)
        worker.run_once()
    finally:
        redirector.close()
        elsewhere.close()
    (delivery,) = store.list_deliveries()
    assert (delivery.status, delivery.last_status_code) == ("dead", 302)
    assert elsewhere.hits == []
