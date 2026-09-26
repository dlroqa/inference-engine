"""``GET /admin/system`` contract and redaction, plus model source redaction (A1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

from engine.api.admin_router import system_summary
from engine.api.models_router import redact_source_ref
from engine.config import RemoteWorkerSpec, Settings, VirtualModelSpec
from engine.main import create_app
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"
LOOPBACK = ("127.0.0.1", 40000)
SECRETS = ("whsec_super_secret", "sk-remote-secret", "hunter2")


def _client(settings: Settings) -> TestClient:
    fake = FakeBackend(tokens=["Hi"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return TestClient(create_app(settings, backend=fake), client=LOOPBACK)


def test_system_contract_defaults(tmp_path: Path) -> None:
    with _client(Settings(data_dir=tmp_path / "data")) as c:
        body = c.get("/admin/system").json()
    assert set(body) == {"build", "readiness", "draining", "switches", "metadata", "routing"}
    assert set(body["build"]) >= {"version", "commit", "built_at"}
    assert body["readiness"]["ready"] is True
    assert body["readiness"]["checks"]["migrations"] == "applied"
    assert body["draining"] is False
    switches = body["switches"]
    assert set(switches) == {
        "allow_model_management",
        "allow_network_downloads",
        "allow_structured_output",
        "diagnostics_enabled",
        "require_auth",
        "webhooks_enabled",
        "client_events_enabled",
        "ip_allowlist_set",
        "grpc_enabled",
    }
    assert all(isinstance(v, bool) for v in switches.values())
    assert body["metadata"] == {"grpc_port": None, "billing_provider": None}
    assert body["routing"]["virtual_models"] == []


def test_system_is_redacted(tmp_path: Path) -> None:
    worker = RemoteWorkerSpec(
        name="gpu-a",
        kind="remote_vllm",
        base_url="https://user:hunter2@vllm.internal:8000",
        model="m",
        api_key="sk-remote-secret",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        billing_provider="stripe",
        stripe_webhook_secret="whsec_super_secret",
        remote_api_key="sk-remote-secret",
        allow_remote_backends=True,
        remote_workers=[worker],
        virtual_models=[VirtualModelSpec(name="smart", policy="route", backends=["primary"])],
        ip_allowlist=["127.0.0.1/32"],
    )
    # Summarize without running the lifespan, so the remote worker is never
    # contacted; redaction is a property of the summary itself.
    app = create_app(settings)
    raw = json.dumps(system_summary(Request({"type": "http", "app": app, "headers": []})))
    for secret in (*SECRETS, "vllm.internal", "https://"):
        assert secret not in raw
    body = json.loads(raw)
    assert body["metadata"]["billing_provider"] == "stripe"
    assert body["switches"]["ip_allowlist_set"] is True
    assert body["routing"]["remote_workers"] == ["gpu-a"]
    assert body["routing"]["virtual_models"] == [{"name": "smart", "policy": "route"}]


def test_redact_source_ref() -> None:
    assert redact_source_ref(None) is None
    assert redact_source_ref("bartowski/x-GGUF/x.gguf@main") == "bartowski/x-GGUF/x.gguf@main"
    assert redact_source_ref("/srv/models/a.gguf") == "/srv/models/a.gguf"
    red = redact_source_ref("https://bob:hunter2@cdn.example.com:8443/m.gguf?token=abc&v=2")
    assert red == "https://cdn.example.com:8443/m.gguf?token=***&v=2"
    signed = redact_source_ref("https://s3.example.com/m.gguf?X-Amz-Signature=deadbeef")
    assert "deadbeef" not in (signed or "")
