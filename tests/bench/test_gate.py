"""The pure §8.1/§8.2 comparison gate (Block 12.2b)."""

from __future__ import annotations

from typing import Any

from engine.bench import HARNESS_VERSION, SCHEMA_VERSION
from engine.bench.gate import RegressionBudget, compare
from engine.bench.report import IdentityStamp, RunReport
from engine.bench.workloads import standard_workload

_MANIFEST = standard_workload(model="m", total_requests=200, seed=1).manifest()


def _identity() -> IdentityStamp:
    return IdentityStamp(
        harness_version=HARNESS_VERSION,
        build_info={"version": "0", "commit": None, "built_at": None},
        python_version="3.12.0",
        platform="test",
        timestamp="2026-01-01T00:00:00+00:00",
        origin="http://host",
    )


def _summary(p95: float | None) -> dict[str, Any]:
    return {"mean": None, "p50": None, "p95": p95, "p99": None}


def _slice(
    *, ttft_p95: float | None = None, total_p95: float | None = None, shed: float | None = None
) -> dict[str, Any]:
    return {
        "attempted": 10,
        "admitted": 10,
        "shed": 0,
        "errors": 0,
        "cancelled": 0,
        "pre_stream_failures": 0,
        "post_stream_failures": 0,
        "shed_rate": shed,
        "error_rate": 0.0,
        "ttft_ms": _summary(ttft_p95),
        "total_ms": _summary(total_p95),
    }


def _report(
    *,
    slices: dict[str, Any],
    error_rate: float = 0.0,
    admitted: int = 100,
    manifest: dict[str, Any] | None = None,
    route_delta: dict[str, Any] | None = None,
) -> RunReport:
    overall = {"admitted": admitted, "error_rate": error_rate, "accepted_rate": 50.0}
    return RunReport(
        identity=_identity(),
        manifest=manifest if manifest is not None else dict(_MANIFEST),
        results={"wall_s": 1.0, "slices": slices, "overall": overall},
        route_delta=route_delta,
        schema_version=SCHEMA_VERSION,
        harness_version=HARNESS_VERSION,
    )


def test_improvement_pass_and_boundary() -> None:
    base = _report(slices={"repeated_prefix": _slice(ttft_p95=100.0, total_p95=100.0)})
    good = _report(slices={"repeated_prefix": _slice(ttft_p95=80.0, total_p95=80.0)})  # 20% better
    out = compare(base, good, target_metric="ttft_p95", target_slice="repeated_prefix")
    assert out.passed, out.reasons

    weak = _report(slices={"repeated_prefix": _slice(ttft_p95=90.0, total_p95=90.0)})  # 10% only
    out = compare(base, weak, target_metric="ttft_p95", target_slice="repeated_prefix")
    assert not out.passed and any("§8.1" in r for r in out.reasons)


def test_equal_values_fail_default_threshold() -> None:
    base = _report(slices={"repeated_prefix": _slice(ttft_p95=100.0, total_p95=100.0)})
    same = _report(slices={"repeated_prefix": _slice(ttft_p95=100.0, total_p95=100.0)})
    assert not compare(base, same, target_metric="ttft_p95", target_slice="repeated_prefix").passed
    # Equal passes only with an explicit zero threshold.
    assert compare(
        base, same, target_metric="ttft_p95", target_slice="repeated_prefix", min_improvement=0.0
    ).passed


def test_higher_is_better_metric() -> None:
    base = _report(slices={"unique": _slice()})
    base.results["overall"]["accepted_rate"] = 40.0  # type: ignore[index]
    cand = _report(slices={"unique": _slice()})
    cand.results["overall"]["accepted_rate"] = 50.0  # type: ignore[index]  # +25%
    assert compare(base, cand, target_metric="accepted_rate", target_slice="overall").passed


def test_error_rate_veto() -> None:
    base = _report(slices={"repeated_prefix": _slice(ttft_p95=100.0)}, error_rate=0.00)
    cand = _report(slices={"repeated_prefix": _slice(ttft_p95=50.0)}, error_rate=0.02)  # +2pp
    out = compare(base, cand, target_metric="ttft_p95", target_slice="repeated_prefix")
    assert not out.passed and any("error rate" in r for r in out.reasons)


def test_latency_veto_on_other_slice() -> None:
    base = _report(
        slices={
            "repeated_prefix": _slice(ttft_p95=100.0, total_p95=100.0),
            "unique": _slice(total_p95=100.0),
        }
    )
    cand = _report(
        slices={
            "repeated_prefix": _slice(ttft_p95=50.0, total_p95=100.0),  # target improves
            "unique": _slice(total_p95=110.0),  # +10% on another slice
        }
    )
    out = compare(base, cand, target_metric="ttft_p95", target_slice="repeated_prefix")
    assert not out.passed and any("p95 latency on unique" in r for r in out.reasons)


def test_saturation_shed_veto() -> None:
    base = _report(
        slices={
            "repeated_prefix": _slice(ttft_p95=100.0),
            "saturation": _slice(shed=0.10),
        }
    )
    cand = _report(
        slices={
            "repeated_prefix": _slice(ttft_p95=50.0),
            "saturation": _slice(shed=0.15),  # +5pp > 2pp budget
        }
    )
    out = compare(base, cand, target_metric="ttft_p95", target_slice="repeated_prefix")
    assert not out.passed and any("shed rate" in r for r in out.reasons)


def test_rejects_incomparable_reports() -> None:
    base = _report(slices={"repeated_prefix": _slice(ttft_p95=100.0)})
    # Manifest mismatch (different seed).
    other_manifest = dict(_MANIFEST)
    other_manifest["seed"] = 999
    cand = _report(slices={"repeated_prefix": _slice(ttft_p95=50.0)}, manifest=other_manifest)
    out = compare(base, cand, target_metric="ttft_p95", target_slice="repeated_prefix")
    assert not out.passed and any("rejected" in r and "manifest" in r for r in out.reasons)

    # Unknown metric.
    good = _report(slices={"repeated_prefix": _slice(ttft_p95=50.0)})
    out = compare(base, good, target_metric="made_up", target_slice="repeated_prefix")
    assert not out.passed and any("unknown target metric" in r for r in out.reasons)

    # Missing target slice.
    out = compare(base, good, target_metric="ttft_p95", target_slice="does_not_exist")
    assert not out.passed and any("missing" in r for r in out.reasons)


def test_zero_baseline_is_rejected_not_divided() -> None:
    base = _report(slices={"repeated_prefix": _slice(ttft_p95=0.0)})
    cand = _report(slices={"repeated_prefix": _slice(ttft_p95=0.0)})
    out = compare(base, cand, target_metric="ttft_p95", target_slice="repeated_prefix")
    assert not out.passed and any("is 0" in r for r in out.reasons)


def test_route_derived_requires_matching_exclusive_source() -> None:
    rd = {"source": "admin_routes_delta", "exclusive_target": True, "output_tps": 100.0}
    base = _report(slices={"unique": _slice()}, route_delta={**rd, "output_tps": 100.0})
    cand = _report(slices={"unique": _slice()}, route_delta={**rd, "output_tps": 130.0})  # +30%
    assert compare(base, cand, target_metric="output_tps", target_slice="overall").passed

    # A non-exclusive candidate is rejected.
    cand2 = _report(
        slices={"unique": _slice()},
        route_delta={
            "source": "admin_routes_delta",
            "exclusive_target": False,
            "output_tps": 130.0,
        },
    )
    out = compare(base, cand2, target_metric="output_tps", target_slice="overall")
    assert not out.passed and any("exclusive" in r for r in out.reasons)


def test_custom_budget() -> None:
    base = _report(slices={"repeated_prefix": _slice(ttft_p95=100.0)}, error_rate=0.0)
    cand = _report(slices={"repeated_prefix": _slice(ttft_p95=50.0)}, error_rate=0.004)  # +0.4pp
    strict = RegressionBudget(error_rate_pp=0.1)
    out = compare(
        base, cand, target_metric="ttft_p95", target_slice="repeated_prefix", budget=strict
    )
    assert not out.passed and any("error rate" in r for r in out.reasons)
