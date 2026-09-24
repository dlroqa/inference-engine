"""Pure §8.1/§8.2 comparison gate (Block 12.2b) — the CI-testable policy gate.

Given a baseline and a candidate :class:`~engine.bench.report.RunReport`, decide
whether the candidate may be enabled per the Block 12 RFC:

* **§8.1** — the candidate must improve the declared *target metric* on the
  declared *target slice* by at least ``min_improvement`` (default 0.15),
  direction-aware (lower is better for TTFT/latency/cost; higher for accepted
  rate / output-TPS).
* **§8.2** — veto if the candidate raises overall error rate by > 0.5 percentage
  points, p95 end-to-end latency by > 5% on any **other** slice, or the
  saturation slice's shed rate by > 2 percentage points.

Incomparable reports (schema/harness/manifest mismatch, missing target,
mismatched metric source, invalid baseline denominator) are **rejected before**
any pass is computed, each with a deterministic reason. This module is pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.bench.report import RunReport

# target metric name -> ("lower" | "higher", is_route_derived)
_DIRECTION: dict[str, str] = {
    "ttft_mean": "lower",
    "ttft_p50": "lower",
    "ttft_p95": "lower",
    "ttft_p99": "lower",
    "latency_mean": "lower",
    "latency_p50": "lower",
    "latency_p95": "lower",
    "latency_p99": "lower",
    "accepted_rate": "higher",
    "output_tps": "higher",
    "cost": "lower",
}
_ROUTE_DERIVED = {"output_tps", "cost"}
# Manifest keys that must match for two runs to be comparable.
_MANIFEST_KEYS = (
    "name",
    "version",
    "seed",
    "model",
    "counts",
    "concurrency",
    "burst_concurrency",
    "warmup_requests",
    "max_tokens",
    "prefix_chars",
    "long_context_chars",
    "stream_mode",
)


@dataclass(frozen=True)
class RegressionBudget:
    """RFC §8.2 budget: absolute points for rate metrics, relative for latency."""

    error_rate_pp: float = 0.5
    latency_pct: float = 0.05
    shed_rate_pp: float = 2.0


@dataclass
class GateOutcome:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, object] = field(default_factory=dict)


def _slice_metrics(report: RunReport, slice_name: str) -> dict[str, object] | None:
    slices = report.results.get("slices", {})
    if not isinstance(slices, dict):
        return None
    row = slices.get(slice_name)
    return row if isinstance(row, dict) else None


def _client_value(report: RunReport, metric: str, slice_name: str) -> float | None:
    """Resolve a client target metric to a numeric value, or None if undefined."""
    if metric == "accepted_rate":
        overall = report.results.get("overall", {})
        val = overall.get("accepted_rate") if isinstance(overall, dict) else None
        return float(val) if isinstance(val, (int, float)) else None
    row = _slice_metrics(report, slice_name)
    if row is None:
        return None
    family, _, stat = metric.partition("_")
    key = {"ttft": "ttft_ms", "latency": "total_ms"}.get(family)
    if key is None:
        return None
    summary = row.get(key)
    if not isinstance(summary, dict):
        return None
    val = summary.get(stat)
    return float(val) if isinstance(val, (int, float)) else None


def _route_value(report: RunReport, metric: str) -> tuple[float | None, str | None, bool]:
    """(value, source, exclusive) for a route-derived metric."""
    rd = report.route_delta
    if not isinstance(rd, dict):
        return None, None, False
    val = rd.get(metric)
    source = rd.get("source")
    exclusive = bool(rd.get("exclusive_target", False))
    num = float(val) if isinstance(val, (int, float)) else None
    return num, (str(source) if source is not None else None), exclusive


def _target_value(report: RunReport, metric: str, slice_name: str) -> float | None:
    if metric in _ROUTE_DERIVED:
        return _route_value(report, metric)[0]
    return _client_value(report, metric, slice_name)


def _p95_latency(report: RunReport, slice_name: str) -> float | None:
    row = _slice_metrics(report, slice_name)
    if row is None:
        return None
    total = row.get("total_ms")
    if not isinstance(total, dict):
        return None
    val = total.get("p95")
    return float(val) if isinstance(val, (int, float)) else None


def _overall_error_rate(report: RunReport) -> tuple[float | None, int]:
    overall = report.results.get("overall", {})
    if not isinstance(overall, dict):
        return None, 0
    admitted = int(overall.get("admitted", 0) or 0)
    rate = overall.get("error_rate")
    return (float(rate) if isinstance(rate, (int, float)) else None), admitted


def _shed_rate(report: RunReport, slice_name: str) -> float | None:
    row = _slice_metrics(report, slice_name)
    if row is None:
        return None
    val = row.get("shed_rate")
    return float(val) if isinstance(val, (int, float)) else None


def _incomparable(
    baseline: RunReport, candidate: RunReport, metric: str, slice_name: str
) -> list[str]:
    reasons: list[str] = []
    if baseline.schema_version != candidate.schema_version:
        reasons.append("schema_version mismatch between reports")
    if baseline.harness_version != candidate.harness_version:
        reasons.append(
            f"harness_version mismatch: {baseline.harness_version!r} vs "
            f"{candidate.harness_version!r}"
        )
    for key in _MANIFEST_KEYS:
        if baseline.manifest.get(key) != candidate.manifest.get(key):
            reasons.append(f"workload manifest mismatch on {key!r}")
    if metric not in _DIRECTION:
        reasons.append(f"unknown target metric {metric!r}")
    if metric in _ROUTE_DERIVED:
        _, b_src, b_excl = _route_value(baseline, metric)
        _, c_src, c_excl = _route_value(candidate, metric)
        if b_src is None or c_src is None:
            reasons.append(f"target metric {metric!r} is route-derived but unavailable")
        elif b_src != c_src:
            reasons.append(f"route metric source mismatch: {b_src!r} vs {c_src!r}")
        elif not (b_excl and c_excl):
            reasons.append(f"route metric {metric!r} requires an exclusive-target run in both")
    else:
        # The target slice must exist in both reports for a slice metric.
        if metric != "accepted_rate" and (
            _slice_metrics(baseline, slice_name) is None
            or _slice_metrics(candidate, slice_name) is None
        ):
            reasons.append(f"target slice {slice_name!r} missing from a report")
    return reasons


def compare(
    baseline: RunReport,
    candidate: RunReport,
    *,
    target_metric: str,
    target_slice: str,
    min_improvement: float = 0.15,
    budget: RegressionBudget | None = None,
) -> GateOutcome:
    """Decide whether ``candidate`` clears the RFC §8.1/§8.2 gate vs ``baseline``."""
    budget = budget or RegressionBudget()

    rejects = _incomparable(baseline, candidate, target_metric, target_slice)
    if rejects:
        return GateOutcome(passed=False, reasons=["rejected: " + r for r in rejects])

    reasons: list[str] = []
    details: dict[str, object] = {}

    # -- §8.1 improvement on the target metric/slice -----------------------
    base_v = _target_value(baseline, target_metric, target_slice)
    cand_v = _target_value(candidate, target_metric, target_slice)
    details["target"] = {
        "metric": target_metric,
        "slice": target_slice,
        "baseline": base_v,
        "candidate": cand_v,
    }
    if base_v is None or cand_v is None:
        return GateOutcome(
            passed=False,
            reasons=[f"rejected: target metric {target_metric!r} undefined in a report"],
            details=details,
        )
    if base_v == 0:
        return GateOutcome(
            passed=False,
            reasons=[f"rejected: baseline {target_metric!r} is 0 (improvement undefined)"],
            details=details,
        )
    direction = _DIRECTION[target_metric]
    improvement = (base_v - cand_v) / base_v if direction == "lower" else (cand_v - base_v) / base_v
    details["improvement"] = improvement
    improved = improvement >= min_improvement
    if not improved:
        reasons.append(
            f"§8.1: {target_metric} on {target_slice} improved {improvement:.3%} "
            f"< required {min_improvement:.3%}"
        )

    # -- §8.2 regression budget -------------------------------------------
    # Overall error rate (absolute percentage points).
    b_err, b_adm = _overall_error_rate(baseline)
    c_err, c_adm = _overall_error_rate(candidate)
    if b_err is None or c_err is None or b_adm == 0 or c_adm == 0:
        return GateOutcome(
            passed=False,
            reasons=["rejected: overall error rate has an invalid (zero-admitted) denominator"],
            details=details,
        )
    err_delta_pp = (c_err - b_err) * 100.0
    details["error_rate_delta_pp"] = err_delta_pp
    if err_delta_pp > budget.error_rate_pp:
        reasons.append(f"§8.2: overall error rate +{err_delta_pp:.2f}pp > {budget.error_rate_pp}pp")

    # p95 latency on every OTHER slice (relative increase).
    other = sorted(s for s in _all_slices(baseline, candidate) if s != target_slice)
    for s in other:
        b_lat = _p95_latency(baseline, s)
        c_lat = _p95_latency(candidate, s)
        if b_lat is None or c_lat is None or b_lat == 0:
            continue  # not assessable for this slice
        rel = (c_lat - b_lat) / b_lat
        if rel > budget.latency_pct:
            reasons.append(f"§8.2: p95 latency on {s} +{rel:.2%} > {budget.latency_pct:.0%}")

    # Saturation-slice shed rate (absolute percentage points), when present.
    b_shed = _shed_rate(baseline, "saturation")
    c_shed = _shed_rate(candidate, "saturation")
    if b_shed is not None and c_shed is not None:
        shed_delta_pp = (c_shed - b_shed) * 100.0
        details["shed_delta_pp"] = shed_delta_pp
        if shed_delta_pp > budget.shed_rate_pp:
            reasons.append(
                f"§8.2: saturation shed rate +{shed_delta_pp:.2f}pp > {budget.shed_rate_pp}pp"
            )

    return GateOutcome(passed=not reasons, reasons=reasons, details=details)


def _all_slices(baseline: RunReport, candidate: RunReport) -> set[str]:
    def names(r: RunReport) -> set[str]:
        slices = r.results.get("slices", {})
        return set(slices) if isinstance(slices, dict) else set()

    return names(baseline) & names(candidate)
