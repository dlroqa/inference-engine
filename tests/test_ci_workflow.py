"""Guards on the CI workflow's aggregate gate and evidence steps.

``ci-success`` is the single required check (and the gate the release workflow
reuses), so every required job must feed it and any non-success result — failed,
cancelled, or skipped — must fail it. The only exception is the container job
when the release calls CI with ``skip-container`` (it builds its own candidate).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
ADVISORY = {"security-audit"}  # documented as advisory (does not gate the build)


def _ci() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _jobs() -> dict[str, Any]:
    return _ci()["jobs"]


def test_every_required_job_feeds_the_aggregate_gate() -> None:
    jobs = _jobs()
    gate = jobs["ci-success"]
    required = set(jobs) - {"ci-success"} - ADVISORY
    assert set(gate["needs"]) == required
    assert "cross-platform" in gate["needs"]
    assert gate["if"] == "always()"


def test_gate_fails_on_any_non_success_result() -> None:
    gate = _jobs()["ci-success"]
    script = "\n".join(step.get("run", "") for step in gate["steps"])
    for job in gate["needs"]:
        assert f"needs.{job}.result" in script, f"gate never reads {job}"
        if job == "container":
            continue
        # Each job is compared against "success" (so failure/cancelled/skipped fail).
        assert re.search(
            rf'\[ "\$\{{\{{ needs\.{re.escape(job)}\.result \}}\}}" != "success" \]', script
        ), f"gate does not require {job} == success"
    # The skip exception is scoped to the container job and the release input.
    assert 'CONTAINER" = "skipped" ] && [ "${{ inputs.skip-container }}" = "true" ]' in script
    assert '[ "$CONTAINER" != "success" ]' in script
    assert script.count("skipped") == 1


def test_cross_platform_matrix_is_intact() -> None:
    job = _jobs()["cross-platform"]
    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["os"]) == {"windows-latest", "macos-latest"}


def test_required_jobs_never_mask_failures() -> None:
    for name, job in _jobs().items():
        if name in ADVISORY:
            continue
        assert "continue-on-error" not in job, name
        for step in job.get("steps", []):
            if step.get("continue-on-error"):
                # Only best-effort artifact uploads (storage quota) may continue.
                assert "upload-artifact" in step.get("uses", ""), (name, step.get("name"))


def test_every_job_records_its_runner_inventory() -> None:
    for name, job in _jobs().items():
        if name == "ci-success":
            continue
        runs = [step.get("run", "") for step in job.get("steps", [])]
        assert any("scripts/runner_inventory.py" in r for r in runs), name


def test_dashboard_smoke_modes_are_explicit() -> None:
    jobs = _jobs()

    def smoke_lines(job: str) -> list[str]:
        return [
            line
            for step in jobs[job]["steps"]
            for line in step.get("run", "").splitlines()
            if "dashboard_smoke.py" in line
        ]

    real = smoke_lines("integration-llama")
    assert real and all("--skip-generation" not in line for line in real)
    package = smoke_lines("build")
    assert package and all("--skip-generation" in line for line in package)


def test_dashboard_lockfile_is_audited_and_reported() -> None:
    runs = [step.get("run", "") for step in _jobs()["dashboard"]["steps"]]
    assert any("npm audit --json" in r and "npm_audit_report.py" in r for r in runs)
