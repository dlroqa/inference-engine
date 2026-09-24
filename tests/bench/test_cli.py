"""CLI entry point + compare exit codes (Block 12.2b). No live server needed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from engine.bench import HARNESS_VERSION, SCHEMA_VERSION
from engine.bench.main import main
from engine.bench.workloads import standard_workload

_MANIFEST = standard_workload(model="m", total_requests=200, seed=1).manifest()


def _summary(p95: float | None) -> dict[str, Any]:
    return {"mean": None, "p50": None, "p95": p95, "p99": None}


def _report_dict(ttft_p95: float, *, error_rate: float = 0.0) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "identity": {
            "harness_version": HARNESS_VERSION,
            "build_info": {},
            "python_version": "3.12",
            "platform": "t",
            "timestamp": "2026-09-24T00:00:00+00:00",
            "origin": "http://host",
        },
        "manifest": _MANIFEST,
        "results": {
            "wall_s": 1.0,
            "slices": {
                "repeated_prefix": {
                    "shed_rate": None,
                    "ttft_ms": _summary(ttft_p95),
                    "total_ms": _summary(100.0),
                }
            },
            "overall": {"admitted": 100, "error_rate": error_rate, "accepted_rate": 50.0},
        },
        "route_delta": None,
        "metric_sources": {"ttft": "client"},
    }


def _write(path: Path, ttft_p95: float, **kw: Any) -> str:
    path.write_text(json.dumps(_report_dict(ttft_p95, **kw)))
    return str(path)


def test_compare_exits_zero_on_improvement(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.json", 100.0)
    cand = _write(tmp_path / "cand.json", 80.0)  # 20% better
    rc = main(
        [
            "compare",
            "--baseline",
            base,
            "--candidate",
            cand,
            "--target-metric",
            "ttft_p95",
            "--target-slice",
            "repeated_prefix",
        ]
    )
    assert rc == 0


def test_compare_exits_nonzero_on_weak_improvement(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.json", 100.0)
    cand = _write(tmp_path / "cand.json", 95.0)  # only 5%
    rc = main(
        [
            "compare",
            "--baseline",
            base,
            "--candidate",
            cand,
            "--target-metric",
            "ttft_p95",
            "--target-slice",
            "repeated_prefix",
        ]
    )
    assert rc == 1


def test_compare_exits_nonzero_on_budget_veto(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.json", 100.0, error_rate=0.0)
    cand = _write(tmp_path / "cand.json", 50.0, error_rate=0.02)  # target improves, error +2pp
    rc = main(
        [
            "compare",
            "--baseline",
            base,
            "--candidate",
            cand,
            "--target-metric",
            "ttft_p95",
            "--target-slice",
            "repeated_prefix",
        ]
    )
    assert rc == 1


def test_compare_reports_error_on_malformed_report(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.json", 100.0)
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json")
    rc = main(
        [
            "compare",
            "--baseline",
            base,
            "--candidate",
            str(bad),
            "--target-metric",
            "ttft_p95",
            "--target-slice",
            "repeated_prefix",
        ]
    )
    assert rc == 2  # distinct from a gate failure (1)


def test_module_entry_point_runs(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.json", 100.0)
    cand = _write(tmp_path / "cand.json", 80.0)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "engine.bench",
            "compare",
            "--baseline",
            base,
            "--candidate",
            cand,
            "--target-metric",
            "ttft_p95",
            "--target-slice",
            "repeated_prefix",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "GATE: PASS" in proc.stdout
