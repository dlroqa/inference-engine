"""The dashboard control-plane smoke (scripts/dashboard_smoke.py), tested here so
its CI role is trustworthy: its ``/admin/system`` validator matches the real
contract and rejects malformed bodies, and real-model mode can never pass by
silently skipping generation."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import running_app
from tests.support.fake_backend import FakeBackend

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "dashboard_smoke", ROOT / "scripts" / "dashboard_smoke.py"
)
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
sys.modules["dashboard_smoke"] = smoke
_spec.loader.exec_module(smoke)

MODEL_ID = "smoke-model"
LOOPBACK = ("127.0.0.1", 40000)


def _loaded_app(tmp_path: Path) -> Any:
    fake = FakeBackend(tokens=["Hi", "!"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(Settings(data_dir=tmp_path / "data", model_id=MODEL_ID), backend=fake)


@pytest.fixture
def real_system(tmp_path: Path) -> dict[str, Any]:
    with TestClient(_loaded_app(tmp_path), client=LOOPBACK) as c:
        body = c.get("/admin/system").json()
    assert isinstance(body, dict)
    return body


def test_validator_accepts_the_real_contract(real_system: dict[str, Any]) -> None:
    assert smoke.validate_system(real_system, require_inference=True) == []
    assert smoke.validate_system(real_system, require_inference=False) == []


def test_validator_requires_inference_only_in_real_model_mode(
    real_system: dict[str, Any],
) -> None:
    body = copy.deepcopy(real_system)
    body["readiness"]["inference"] = {"available": False, "reason": "no model loaded"}
    assert smoke.validate_system(body, require_inference=False) == []
    assert smoke.validate_system(body, require_inference=True) == [
        "inference not available after model load"
    ]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda b: b.pop("routing"), "top-level keys"),
        (lambda b: b["switches"].__setitem__("grpc_enabled", "false"), "not a real boolean"),
        (lambda b: b["switches"].pop("diagnostics_enabled"), "agreed set"),
        (lambda b: b.__setitem__("draining", "no"), "draining is not a boolean"),
        (lambda b: b["metadata"].__setitem__("grpc_port", "50051"), "grpc_port"),
        (lambda b: b["metadata"].__setitem__("grpc_port", 50051), "while gRPC is disabled"),
        (lambda b: b["metadata"].__setitem__("billing_provider", 1), "billing_provider"),
        (lambda b: b["readiness"].__setitem__("ready", 1), "readiness.ready"),
        (lambda b: b["routing"].__setitem__("workload_rule_count", True), "workload_rule_count"),
        (lambda b: b["routing"].__setitem__("virtual_models", [{"name": "x"}]), "virtual_models"),
    ],
)
def test_validator_rejects_malformed_bodies(
    real_system: dict[str, Any], mutate: Any, expected: str
) -> None:
    body = copy.deepcopy(real_system)
    mutate(body)
    problems = smoke.validate_system(body, require_inference=False)
    assert any(expected in p for p in problems), problems


def test_validator_rejects_non_objects() -> None:
    assert smoke.validate_system([], require_inference=False) == ["body is not an object"]


def test_real_model_mode_passes_with_a_loaded_model(tmp_path: Path, capsys: Any) -> None:
    with running_app(_loaded_app(tmp_path)) as base:
        rc = smoke.Smoke(base, None).run(skip_generation=False)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[PASS] generation" in out
    assert "[PASS] system contract + inference available" in out
    assert "[SKIP]" not in out


def test_real_model_mode_fails_without_a_model(tmp_path: Path, capsys: Any) -> None:
    # No model configured: the smoke must fail, never skip generation silently.
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with running_app(app) as base:
        rc = smoke.Smoke(base, None).run(skip_generation=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] model loaded (real-model mode)" in out
    assert "[FAIL] generation" in out
    assert "[FAIL] system contract + inference available" in out


def test_no_model_mode_still_checks_the_system_contract(tmp_path: Path, capsys: Any) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with running_app(app) as base:
        rc = smoke.Smoke(base, None).run(skip_generation=True)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[SKIP] generation — explicit --skip-generation" in out
    assert "[PASS] system contract (no-model mode)" in out


# --- A3a: System + Backends & Routing endpoints --------------------------------


def test_view_validators_accept_the_real_contracts(tmp_path: Path) -> None:
    with TestClient(_loaded_app(tmp_path), client=LOOPBACK) as c:
        c.post(
            "/v1/chat/completions",
            json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
        )
        backends = c.get("/admin/backends").json()
        routes = c.get("/admin/routes").json()
        plan = c.post("/admin/route/plan", json={"model": MODEL_ID}).json()
    assert smoke.validate_backends(backends, require_ready=True) == []
    assert smoke.validate_routes(routes, model=MODEL_ID) == []
    assert smoke.validate_plan(plan, model=MODEL_ID, require_choice=True) == []


def test_view_validators_reject_malformed_or_unproven_bodies(tmp_path: Path) -> None:
    with TestClient(_loaded_app(tmp_path), client=LOOPBACK) as c:
        backends = c.get("/admin/backends").json()
        routes = c.get("/admin/routes").json()  # no traffic yet
        plan = c.post("/admin/route/plan", json={"model": "nope"}).json()
    leaky = copy.deepcopy(backends)
    leaky["backends"][0]["base_url"] = "http://user:pass@host"
    assert "a backend row exposes a URL or key field" in smoke.validate_backends(
        leaky, require_ready=False
    )
    wrong_count = copy.deepcopy(backends)
    wrong_count["count"] = 5
    assert smoke.validate_backends(wrong_count, require_ready=False) == [
        "backends count does not match the list"
    ]
    assert smoke.validate_routes(routes, model=MODEL_ID) == [
        "no route row records the smoke's generation"
    ]
    assert smoke.validate_routes(routes, model=None) == []
    assert "route plan chose no backend for the loaded model" in smoke.validate_plan(
        plan, model="nope", require_choice=True
    )
    assert smoke.validate_plan(plan, model="other", require_choice=False) == [
        "route plan is for a different model"
    ]


def test_real_model_mode_checks_the_view_endpoints(tmp_path: Path, capsys: Any) -> None:
    with running_app(_loaded_app(tmp_path)) as base:
        rc = smoke.Smoke(base, None).run(skip_generation=False)
    out = capsys.readouterr().out
    assert rc == 0, out
    for line in (
        "[PASS] backends pool",
        "[PASS] route plan dry run",
        "[PASS] route plan rejects an unknown feature (400)",
        "[PASS] per-route metrics",
        "[PASS] diagnostics bundle (enabled)",
    ):
        assert line in out, out
    # Only names and statuses are printed: never the bundle.
    assert "recent_logs" not in out and "hardware" not in out


def test_smoke_expects_a_refusal_when_diagnostics_are_off(tmp_path: Path, capsys: Any) -> None:
    fake = FakeBackend(tokens=["Hi"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    settings = Settings(data_dir=tmp_path / "data", model_id=MODEL_ID, diagnostics_enabled=False)
    with running_app(create_app(settings, backend=fake)) as base:
        rc = smoke.Smoke(base, None).run(skip_generation=False)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[PASS] diagnostics refused (disabled)" in out
