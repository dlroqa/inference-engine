"""End-to-end driver run against a real Uvicorn engine with a FakeBackend (12.2b).

Uses a real server (not in-process ASGI) so streaming, first-content TTFT, and
client-initiated cancellation behave realistically. Skips cleanly without httpx.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("httpx")

from engine.auth.keys import KeyStore  # noqa: E402
from engine.bench.driver import run_workload  # noqa: E402
from engine.bench.metrics import aggregate  # noqa: E402
from engine.bench.report import IdentityStamp, RunReport  # noqa: E402
from engine.bench.workloads import smoke_workload  # noqa: E402
from engine.config import Settings  # noqa: E402
from engine.main import create_app  # noqa: E402
from tests.api.conftest import running_app  # noqa: E402
from tests.support.fake_backend import FakeBackend  # noqa: E402

MODEL = "bench-model"


def _app(tmp_path):
    # Many delayed tokens so a 1-chunk cancellation happens well before completion.
    fake = FakeBackend(tokens=["tok"] * 40, per_token_delay=0.01, model_id=MODEL)
    asyncio.run(fake.load())
    return create_app(Settings(data_dir=tmp_path / "data", model_id=MODEL), backend=fake)


def _operator_key(tmp_path) -> str:
    # /admin/routes needs a real operator key: an invalid token is refused even
    # on loopback (credentials are checked before the loopback dev exception).
    _record, token = KeyStore(Settings(data_dir=tmp_path / "data").db_path).create(label="op")
    return token


def test_driver_runs_and_reports_safely(tmp_path) -> None:
    app = _app(tmp_path)
    with running_app(app) as base_url:
        op = _operator_key(tmp_path)
        samples, wall_s, route_delta = asyncio.run(
            run_workload(
                base_url, smoke_workload(model=MODEL), admin_api_key=op, exclusive_target=True
            )
        )

    assert wall_s > 0
    by_slice = {s.slice: [x for x in samples if x.slice == s.slice] for s in samples}
    # Unique requests succeeded with an observed first-content TTFT.
    unique = by_slice["unique"]
    assert unique and all(s.outcome == "ok" for s in unique)
    assert all(s.ttft_ms is not None and s.observed_first_content for s in unique)
    # The cancellation slice is recorded as a client cancellation, not an error.
    cancel = by_slice["cancellation"]
    assert cancel and all(s.outcome == "cancelled" for s in cancel)
    assert all(s.content_chunks >= 1 for s in cancel)

    # Route-snapshot delta is available under an exclusive-target run on loopback.
    assert route_delta is not None and route_delta["source"] == "admin_routes_delta"
    assert route_delta["exclusive_target"] is True

    # A full report carries no prompt/response content.
    report = RunReport(
        identity=IdentityStamp(
            harness_version="1.0.0",
            build_info={},
            python_version="3.12",
            platform="t",
            timestamp="2026-09-24T00:00:00+00:00",
            origin="http://127.0.0.1",
        ),
        manifest=smoke_workload(model=MODEL).manifest(),
        results=aggregate(samples, wall_s=wall_s),
        route_delta=route_delta,
    )
    blob = report.to_json()
    assert "Unique request" not in blob and "Write a long essay" not in blob


def test_cli_run_writes_a_valid_report(tmp_path) -> None:
    from engine.bench.main import main
    from engine.bench.report import RunReport

    app = _app(tmp_path)
    out = tmp_path / "candidate.json"
    with running_app(app) as base_url:
        op = _operator_key(tmp_path)
        rc = main(
            [
                "run",
                "--base-url",
                base_url,
                "--workload",
                "smoke",
                "--model",
                MODEL,
                "--out",
                str(out),
                "--admin-api-key",
                op,
                "--exclusive-target",
            ]
        )
    assert rc == 0
    report = RunReport.from_json(out.read_text())
    assert report.manifest["name"] == "smoke"
    assert report.identity.origin.startswith("http://127.0.0.1")
    assert report.metric_sources["output_tps"] == "admin_routes_delta"


def test_driver_marks_unknown_model_pre_stream_failure(tmp_path) -> None:
    app = _app(tmp_path)
    with running_app(app) as base_url:
        samples, wall_s, _ = asyncio.run(
            run_workload(base_url, smoke_workload(model="not-served"), exclusive_target=False)
        )
    # An unknown model is a 404 before any content -> pre-stream failure, not a crash.
    assert samples and all(s.failure_phase == "pre_stream" for s in samples if s.outcome == "error")
    res = aggregate(samples, wall_s=wall_s)["overall"]
    assert res["pre_stream_failures"] >= 1
