"""Client-scoped account contract: /client/* with strict authorization isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from engine.auth.keys import KeyStore
from engine.billing.store import BillingStore
from engine.config import Settings
from engine.main import create_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"


def _app(tmp_path: Path, **overrides: object) -> tuple[object, Settings]:
    settings = Settings(data_dir=tmp_path, require_auth=True, **overrides)  # type: ignore[arg-type]
    fake = FakeBackend(tokens=["a", "b"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def _client_with_key(
    settings: Settings, client_id: str, *, plan_id: str | None = None, **plan_kwargs: object
) -> str:
    billing = BillingStore(settings.db_path)  # type: ignore[arg-type]
    keys = KeyStore(settings.db_path)  # type: ignore[arg-type]
    billing.create_client(id=client_id, external_ref=f"cus_{client_id}", email=f"{client_id}@x.io")
    record, token = keys.create(label=client_id)
    billing.attach_key(record.id, client_id)
    if plan_id is not None:
        billing.upsert_plan(id=plan_id, name=plan_id, **plan_kwargs)  # type: ignore[arg-type]
        billing.upsert_subscription(
            id=f"s_{client_id}",
            client_id=client_id,
            plan_id=plan_id,
            provider="stripe",
            provider_sub_id=f"sub_{client_id}",
            status="active",
        )
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_returns_own_account(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _client_with_key(settings, "c1", plan_id="pro", quota_5h_cu=10, quota_weekly_cu=100)
        resp = client.get("/client/me", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "c1" and body["email"] == "c1@x.io"
        assert body["plan"]["id"] == "pro" and body["plan"]["quota_weekly_cu"] == 100


def test_requires_key(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/client/me").status_code == 401
        assert client.get("/client/me", headers=_auth("sk-ie-nope")).status_code == 401


def test_operator_or_unowned_key_forbidden(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        # A valid key with no owning client is not a client credential.
        _, token = KeyStore(settings.db_path).create(label="op")  # type: ignore[arg-type]
        resp = client.get("/client/me", headers=_auth(token))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "not_a_client_key"


def test_suspended_client_forbidden(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _client_with_key(settings, "c1", plan_id="pro", quota_5h_cu=10, quota_weekly_cu=100)
        BillingStore(settings.db_path).set_client_status("c1", "suspended")  # type: ignore[arg-type]
        resp = client.get("/client/me", headers=_auth(token))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "key_suspended"


def test_isolation_between_clients(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token_a = _client_with_key(settings, "a", plan_id="pa", quota_5h_cu=1, quota_weekly_cu=2)
        token_b = _client_with_key(settings, "b", plan_id="pb", quota_5h_cu=3, quota_weekly_cu=4)
        me_a = client.get("/client/me", headers=_auth(token_a)).json()
        me_b = client.get("/client/me", headers=_auth(token_b)).json()
        assert me_a["id"] == "a" and me_b["id"] == "b"
        # Each sees only their own plan; no request field can select the other.
        assert me_a["plan"]["id"] == "pa" and me_b["plan"]["id"] == "pb"
        # A query param naming the other client is ignored (identity is the key).
        spoof = client.get("/client/me?client_id=b", headers=_auth(token_a)).json()
        assert spoof["id"] == "a"


def test_plan_and_usage_reflect_enforcement(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        token = _client_with_key(
            settings, "c1", plan_id="pro", quota_5h_cu=1000, quota_weekly_cu=5000
        )
        plan = client.get("/client/plan", headers=_auth(token)).json()
        assert plan["entitled"] is True
        assert plan["plan"]["quota_5h_cu"] == 1000 and plan["plan"]["quota_weekly_cu"] == 5000

        # Serve one request, then usage should be non-zero and within the plan limits.
        chat = client.post(
            "/v1/chat/completions",
            json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
            headers=_auth(token),
        )
        assert chat.status_code == 200
        usage = client.get("/client/usage", headers=_auth(token)).json()
        assert usage["cu_weekly"]["limit_cu"] == 5000
        assert usage["cu_weekly"]["used_cu"] > 0
        assert usage["client_weekly_cu"] == usage["cu_weekly"]["used_cu"]


def test_services_respects_allowed_models(tmp_path: Path) -> None:
    app, settings = _app(tmp_path)
    with TestClient(app) as client:
        # Unrestricted plan sees the served model.
        open_token = _client_with_key(
            settings, "open", plan_id="all", quota_5h_cu=10, quota_weekly_cu=10
        )
        svc = client.get("/client/services", headers=_auth(open_token)).json()
        assert MODEL_ID in svc["models"]

        # Restricted plan (allowed_models excludes the served model) sees none.
        restricted = _client_with_key(
            settings,
            "restricted",
            plan_id="limited",
            quota_5h_cu=10,
            quota_weekly_cu=10,
            allowed_models=["some-other-model"],
        )
        svc2 = client.get("/client/services", headers=_auth(restricted)).json()
        assert MODEL_ID not in svc2["models"]


def test_openapi_publishes_client_paths_and_bearer_scheme(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
        assert "/client/me" in spec["paths"]
        assert "/client/usage" in spec["paths"]
        assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
