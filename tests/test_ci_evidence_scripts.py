"""Unit tests for the CI evidence helpers: scripts/measure.py and
scripts/npm_audit_report.py. They must label what they measure honestly and
never turn a failed audit into a clean result."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


measure = _load("measure")
audit = _load("npm_audit_report")


# --- measure.py -------------------------------------------------------------


def test_vmhwm_parsing() -> None:
    status = "Name:\tpython\nVmPeak:\t 900000 kB\nVmHWM:\t  524288 kB\nVmRSS:\t 1000 kB\n"
    assert measure.parse_vmhwm(status) == 512.0
    assert measure.parse_vmhwm("Name:\tx\n") is None


def test_maxrss_units_are_normalized() -> None:
    assert measure.maxrss_to_mib(2048, "Linux") == 2.0  # KiB
    assert measure.maxrss_to_mib(2 * 1024 * 1024, "Darwin") == 2.0  # bytes


@pytest.mark.parametrize("code", [0, 3])
def test_run_propagates_the_exit_status(code: int, capsys: Any) -> None:
    rc = measure.main(
        ["run", "--label", "t", "--", sys.executable, "-c", f"raise SystemExit({code})"]
    )
    assert rc == code
    out = capsys.readouterr().out
    assert "MEASURE t:" in out and f"exit={code}" in out


def test_pid_mode_reports_unmeasured_for_a_missing_process(capsys: Any) -> None:
    assert measure.main(["pid", "--label", "gone", "999999999"]) == 0
    assert "unmeasured" in capsys.readouterr().out


# --- npm_audit_report.py ----------------------------------------------------

_REPORT = {
    "auditReportVersion": 2,
    "vulnerabilities": {
        "vitest": {
            "name": "vitest",
            "severity": "critical",
            "isDirect": True,
            "via": [
                {
                    "source": 1,
                    "url": "https://github.com/advisories/GHSA-test",
                    "title": "RCE",
                    "severity": "critical",
                    "range": "<2.1.9",
                }
            ],
            "range": "<2.1.9",
            "fixAvailable": True,
        },
        "esbuild": {
            "name": "esbuild",
            "severity": "moderate",
            "isDirect": False,
            "via": [
                {
                    "url": "https://github.com/advisories/GHSA-e",
                    "title": "dev server",
                    "severity": "moderate",
                    "range": "<=0.24.2",
                }
            ],
            "range": "<=0.24.2",
            "fixAvailable": {"name": "vite", "version": "7.0.0", "isSemVerMajor": True},
        },
        "vite": {
            "name": "vite",
            "severity": "moderate",
            "isDirect": True,
            "via": ["esbuild"],
            "range": "0.11.0 - 6.1.6",
            "fixAvailable": {"name": "vite", "version": "7.0.0", "isSemVerMajor": True},
        },
    },
    "metadata": {"vulnerabilities": {"critical": 1, "moderate": 2, "total": 3}},
}


def test_completed_audit_lists_direct_transitive_and_fixes() -> None:
    summary = audit.summarize(_REPORT, {"devDependencies": {"vitest": "^2", "vite": "^5"}})
    assert summary["status"] == "completed"
    rows = {r["package"]: r for r in summary["packages"]}
    assert rows["vitest"]["direct"] is True
    assert rows["esbuild"]["direct"] is False
    assert rows["vite"]["via_packages"] == ["esbuild"]
    assert rows["vitest"]["fix"].startswith("yes")
    assert "semver-major" in rows["esbuild"]["fix"]
    text = audit.render(summary)
    assert "critical=1" in text and "GHSA-test" in text


def test_failed_audit_is_not_a_clean_result(tmp_path: Path) -> None:
    report = tmp_path / "a.json"
    report.write_text(json.dumps({"error": {"code": "ENOTFOUND", "summary": "network"}}))
    assert audit.main([str(report)]) == 2
    assert audit.summarize({"error": {"summary": "x"}})["status"] == "failed"


def test_garbage_is_invalid(tmp_path: Path) -> None:
    report = tmp_path / "a.json"
    report.write_text("not json")
    assert audit.main([str(report)]) == 2
    assert audit.summarize([])["status"] == "invalid"


def test_clean_audit_passes(tmp_path: Path) -> None:
    report = tmp_path / "a.json"
    report.write_text(json.dumps({"vulnerabilities": {}, "metadata": {"vulnerabilities": {}}}))
    assert audit.main([str(report)]) == 0
