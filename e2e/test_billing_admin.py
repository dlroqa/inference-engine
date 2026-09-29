"""Billing and webhook administration in a real browser against a real engine (A3b).

Required, never skipped. The CI engine runs with webhooks enabled, egress to
127.0.0.1 allowed, one delivery attempt, a fast delivery poll, and a Stripe
signing secret (``IE_E2E_STRIPE_SECRET``) so this test can emit real lifecycle
events through the signed inbound webhook.

Every write made through the UI is checked on the server (admin API, audit log),
and the one-time secrets are checked by their real effect: the client key's
token authenticates as that client, and the signing secrets shown once verify
the deliveries a loopback receiver gets, with an independent Standard Webhooks
check (not the engine's own verifier). No token, secret or receiver body is
printed; screenshots go through ``safe_screenshot``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import re
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import Engine, _required, login, mask, purge_key, safe_screenshot


# --- a loopback webhook receiver ----------------------------------------------------


class Receiver:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self._lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                with outer._lock:
                    outer.requests.append(
                        {
                            "id": self.headers.get("webhook-id"),
                            "timestamp": self.headers.get("webhook-timestamp"),
                            "signature": self.headers.get("webhook-signature") or "",
                            "body": body,
                        }
                    )
                    status = outer.status
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait_for(self, count: int, timeout: float = 30.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.requests) >= count:
                    return list(self.requests)
            time.sleep(0.2)
        raise AssertionError(f"receiver got {len(self.requests)} of {count} deliveries in {timeout} s")

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    r = Receiver()
    try:
        yield r
    finally:
        r.close()


def signed_by(secret: str, delivery: dict[str, Any]) -> bool:
    """Standard Webhooks: base64(HMAC-SHA256(base64-decoded key, "id.ts.body"))."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    content = f"{delivery['id']}.{delivery['timestamp']}.{delivery['body']}".encode()
    expected = base64.b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode()
    tokens = [t.partition(",")[2] for t in delivery["signature"].split() if t.startswith("v1,")]
    return any(hmac.compare_digest(t, expected) for t in tokens)


def stripe_event(engine: Engine, event_type: str, customer: str, status: str = "active") -> None:
    """Emit a real lifecycle event through the signed inbound Stripe webhook."""
    secret = _required("IE_E2E_STRIPE_SECRET")
    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": event_type,
        "data": {"object": {"id": f"sub_{customer}", "customer": customer, "status": status}},
    }
    raw = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    with engine.api() as c:
        resp = c.post(
            "/billing/webhooks/stripe",
            content=raw,
            headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"},
        )
    assert resp.status_code == 200, resp.status_code
    assert resp.json()["status"] == "processed"


def audit_actions(engine: Engine) -> list[dict[str, Any]]:
    with engine.api(engine.operator_key) as c:
        return list(c.get("/admin/audit", params={"limit": 200}).json()["events"])


def open_clients(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="Clients").click()
    expect(page.get_by_role("heading", name="Clients", exact=True)).to_be_visible()


def confirm(page: Page, button: str) -> None:
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name=button, exact=True).click()
    expect(dialog).to_be_hidden()


# --- plans, client, one-time client key -------------------------------------------------


def test_plans_client_and_one_time_client_key(page: Page, engine: Engine) -> None:
    plan_id = f"e2e-{uuid.uuid4().hex[:6]}"
    ref = f"cus_e2e_{uuid.uuid4().hex[:8]}"
    open_clients(page, engine)

    # Plans: create, then update through the confirmation dialog.
    page.get_by_role("tab", name="Plans").click()
    page.get_by_label("Plan id").fill(plan_id)
    page.get_by_label("Name", exact=True).fill("E2E plan")
    page.get_by_label("Weekly quota (CU)").fill("500")
    page.get_by_role("button", name=re.compile(r"^Create plan")).click()
    expect(page.get_by_text(f"Created plan {plan_id}.")).to_be_visible()
    page.get_by_role("button", name=f"Edit plan {plan_id}").click()
    page.get_by_label("Weekly quota (CU)").fill("750")
    page.get_by_role("button", name=re.compile(r"^Update plan")).click()
    confirm(page, "Update plan")
    expect(page.get_by_text(f"Updated plan {plan_id}.")).to_be_visible()
    with engine.api(engine.operator_key) as c:
        plans = {p["id"]: p for p in c.get("/admin/billing/plans").json()["plans"]}
    assert plans[plan_id]["quota_weekly_cu"] == 750

    # Create a client; the view selects it.
    page.get_by_role("tab", name="Clients").click()
    page.get_by_label("Billing reference (optional)").fill(ref)
    page.get_by_role("button", name=re.compile(r"^Create client$")).click()
    expect(page).to_have_url(re.compile(r"#/clients/[0-9a-f]{32}$"))
    client_id = page.url.rsplit("/", 1)[1]
    with engine.api(engine.operator_key) as c:
        clients = {x["id"]: x for x in c.get("/admin/billing/clients").json()["clients"]}
    assert clients[client_id]["external_ref"] == ref

    # A client key: the token is shown once and authenticates as this client.
    page.get_by_label("Key label (optional)").fill("e2e-ui-client-key")
    page.get_by_role("button", name=re.compile(r"^Create client key")).click()
    box = page.get_by_test_id("new-client-token")
    expect(box).to_be_visible()
    token = box.inner_text().strip()
    mask(token)
    key_id = None
    try:
        with engine.api(token) as c:
            me = c.get("/client/me")
            assert me.status_code == 200 and me.json()["id"] == client_id
            denied = c.get("/admin/keys")
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "operator_role_required"
        created = [e for e in audit_actions(engine) if e["action"] == "key.create"]
        match = [e for e in created if (e.get("detail") or {}).get("client_id") == client_id]
        assert match, "no key.create audit event for the client key"
        key_id = match[0]["target"]

        # Dismissed, and absent after a reload and after a new sign-in.
        page.get_by_role("button", name="I have saved it").click()
        expect(box).to_be_hidden()
        safe_screenshot(page, "a3b-client-key-dismissed", [engine.operator_key, token])
        page.reload()
        expect(page.get_by_role("button", name=re.compile(r"^Create client key"))).to_be_visible()
        assert token not in page.content()

        audit = {e["action"] for e in audit_actions(engine)}
        assert {"billing.plan.upsert", "billing.client.create", "key.create"} <= audit
        assert all(token not in json.dumps(e) for e in audit_actions(engine))
    finally:
        if key_id:
            purge_key(engine, key_id)


def test_client_key_token_does_not_survive_a_key_change(page: Page, engine: Engine) -> None:
    open_clients(page, engine)
    page.locator(f'button.rowbtn[data-client-id="{engine.client_id}"]').click()
    page.get_by_role("button", name=re.compile(r"^Create client key")).click()
    box = page.get_by_test_id("new-client-token")
    expect(box).to_be_visible()
    token = box.inner_text().strip()
    mask(token)
    try:
        # Changing the key ends the session: the new session never shows it.
        page.get_by_role("button", name="Change key").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Operator API key").fill(engine.operator_key)
        dialog.get_by_role("button", name="Continue").click()
        expect(page.get_by_role("button", name=re.compile(r"^Create client key"))).to_be_visible()
        assert token not in page.content()
        safe_screenshot(page, "a3b-after-key-change", [engine.operator_key, token])
    finally:
        with engine.api(engine.operator_key) as c:
            keys = c.get("/admin/keys").json()["keys"]
        for k in keys:
            if token.startswith(k["prefix"]):
                purge_key(engine, k["id"])


# --- webhook endpoints, signed deliveries, rotation, disable, replay, delete ---------------------


def deliveries_for(engine: Engine, endpoint_id: str) -> list[dict[str, Any]]:
    with engine.api(engine.operator_key) as c:
        resp = c.get("/admin/billing/webhooks/deliveries", params={"endpoint_id": endpoint_id})
    return list(resp.json()["deliveries"])


def wait_delivery(engine: Engine, endpoint_id: str, event_type: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for d in deliveries_for(engine, endpoint_id):
            if d["event_type"] == event_type and d["status"] == status:
                return d
        time.sleep(0.3)
    raise AssertionError(f"no {status} {event_type} delivery within 30 s")


def test_webhook_endpoint_lifecycle_with_real_deliveries(
    page: Page, engine: Engine, receiver: Receiver
) -> None:
    ref = f"cus_e2e_{uuid.uuid4().hex[:8]}"
    with engine.api(engine.operator_key) as c:
        client_id = c.post("/admin/billing/clients", json={"external_ref": ref}).json()["id"]
    login(page, engine)
    page.goto(f"{engine.dashboard}#/clients/{client_id}")
    expect(page.get_by_role("heading", name=f"Client {client_id[:12]}")).to_be_visible()

    # Add the endpoint; its signing secret is shown once.
    page.get_by_label("URL").fill(receiver.url)
    page.get_by_role("button", name=re.compile(r"^Add endpoint")).click()
    secret_box = page.get_by_test_id("new-endpoint-secret")
    expect(secret_box).to_be_visible()
    first_secret = secret_box.inner_text().strip()
    mask(first_secret)
    with engine.api(engine.operator_key) as c:
        (endpoint,) = c.get("/admin/billing/webhooks/endpoints", params={"client_id": client_id}).json()[
            "endpoints"
        ]
    assert endpoint["url"] == receiver.url and endpoint["disabled"] is False
    endpoint_id = endpoint["id"]
    page.get_by_role("button", name="I have saved it").click()
    secrets = [engine.operator_key, first_secret]

    # A real lifecycle event is delivered and verifies with the secret shown once.
    stripe_event(engine, "customer.subscription.created", ref)
    (first,) = receiver.wait_for(1)
    assert signed_by(first_secret, first)
    wait_delivery(engine, endpoint_id, "subscription.activated", "succeeded")
    page.get_by_role("button", name="Refresh").last.click()
    row = page.locator("tr", has_text="subscription.activated")
    expect(row.first).to_contain_text("succeeded")

    # Rotate: the new secret is shown once with its grace period, and during the
    # grace period a delivery verifies with both the new and the previous secret.
    page.get_by_role("button", name=f"Rotate secret for endpoint {receiver.url}").click()
    confirm(page, "Rotate secret")
    rotated_box = page.get_by_test_id("rotated-secret")
    expect(rotated_box).to_be_visible()
    expect(page.get_by_test_id("rotation-grace")).to_contain_text("1 hour grace period")
    new_secret = rotated_box.inner_text().strip()
    mask(new_secret)
    secrets.append(new_secret)
    assert new_secret != first_secret
    stripe_event(engine, "customer.subscription.updated", ref)
    second = receiver.wait_for(2)[1]
    assert signed_by(new_secret, second) and signed_by(first_secret, second)
    page.get_by_role("button", name="I have saved it").click()
    safe_screenshot(page, "a3b-endpoint-rotated", secrets)

    # Disable (confirmed): the endpoint gets no new events.
    page.get_by_role("button", name=f"Disable endpoint {receiver.url}").click()
    confirm(page, "Disable endpoint")
    expect(page.get_by_role("button", name=f"Enable endpoint {receiver.url}")).to_be_visible()
    before = len(deliveries_for(engine, endpoint_id))
    stripe_event(engine, "customer.subscription.updated", ref)
    time.sleep(2)
    assert len(deliveries_for(engine, endpoint_id)) == before
    assert len(receiver.requests) == 2

    # Enable (no confirmation needed).
    page.get_by_role("button", name=f"Enable endpoint {receiver.url}").click()
    expect(page.get_by_role("button", name=f"Disable endpoint {receiver.url}")).to_be_visible()

    # A failed delivery is dead after its one attempt; Replay sends it again.
    receiver.status = 500
    stripe_event(engine, "customer.subscription.updated", ref, status="past_due")
    dead = wait_delivery(engine, endpoint_id, "subscription.updated", "dead")
    assert dead["last_status_code"] == 500
    receiver.status = 200
    page.get_by_label("Status").select_option("dead")
    replay = page.get_by_role("button", name=re.compile(rf"^Replay subscription\.updated delivery {dead['id'][:8]}"))
    expect(replay).to_be_enabled()
    replay.click()
    confirm(page, "Replay delivery")
    replayed = wait_delivery(engine, endpoint_id, "subscription.updated", "succeeded")
    assert replayed["id"] == dead["id"]
    ids = [r["id"] for r in receiver.requests]
    assert ids.count(ids[-1]) == 2, "the replay reuses the delivery's webhook-id"

    # Delete (confirmed): the endpoint and its history are gone.
    page.get_by_role("button", name=f"Delete endpoint {receiver.url}").click()
    confirm(page, "Delete endpoint")
    expect(page.get_by_text("No webhook endpoints.")).to_be_visible()
    with engine.api(engine.operator_key) as c:
        left = c.get("/admin/billing/webhooks/endpoints", params={"client_id": client_id}).json()
    assert left["endpoints"] == []

    events = [e for e in audit_actions(engine) if e["target"] in (endpoint_id, dead["id"])]
    actions = {e["action"] for e in events}
    assert {
        "webhook.endpoint.create",
        "webhook.endpoint.rotate_secret",
        "webhook.endpoint.disable",
        "webhook.endpoint.enable",
        "webhook.delivery.replay",
        "webhook.endpoint.delete",
    } <= actions
    dumped = json.dumps(audit_actions(engine))
    assert first_secret not in dumped and new_secret not in dumped and "/hook" not in dumped
    safe_screenshot(page, "a3b-endpoint-deleted", secrets)


def test_endpoint_refusals_are_explained(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.goto(f"{engine.dashboard}#/clients/{engine.client_id}")
    url = page.get_by_label("URL")
    add = page.get_by_role("button", name=re.compile(r"^Add endpoint"))
    # A non-ASCII (IDN) host is refused before any egress check.
    url.fill("http://bücher.example.com/hook")
    add.click()
    expect(page.get_by_role("alert")).to_contain_text("xn--")
    # A host outside egress_allowlist is refused.
    url.fill("http://example.com/hook")
    add.click()
    expect(page.get_by_role("alert")).to_contain_text("egress_allowlist")
    with engine.api(engine.operator_key) as c:
        listed = c.get("/admin/billing/webhooks/endpoints", params={"client_id": engine.client_id}).json()
    assert all("example.com" not in e["url"] for e in listed["endpoints"])
