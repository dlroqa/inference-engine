"""Unit tests for the per-route cost + performance accumulator (Block 10.5b)."""

from __future__ import annotations

from engine.telemetry.route_metrics import RouteMetrics


def _record(m: RouteMetrics, **kw: object) -> None:
    base = dict(
        model="fast",
        backend="a",
        prompt_tokens=0,
        completion_tokens=0,
        cost=0.0,
        total_ms=None,
        ttft_ms=None,
        error=False,
        cancelled=False,
    )
    base.update(kw)
    m.record(**base)  # type: ignore[arg-type]


def test_records_tokens_cost_and_latency_averages() -> None:
    m = RouteMetrics()
    _record(m, prompt_tokens=100, completion_tokens=50, cost=0.15, total_ms=200.0, ttft_ms=40.0)
    _record(m, prompt_tokens=200, completion_tokens=10, cost=0.05, total_ms=400.0, ttft_ms=60.0)
    snap = m.snapshot()
    row = snap["routes"][0]
    assert row["model"] == "fast" and row["backend"] == "a"
    assert row["requests"] == 2
    assert row["prompt_tokens"] == 300
    assert row["completion_tokens"] == 60
    assert row["cost"] == 0.2
    assert row["success_rate"] == 1.0
    assert row["avg_total_ms"] == 300.0
    assert row["avg_ttft_ms"] == 50.0


def test_errors_and_cancels_excluded_from_latency_and_success() -> None:
    m = RouteMetrics()
    _record(m, total_ms=100.0, ttft_ms=10.0)  # ok
    _record(m, error=True, total_ms=999.0, ttft_ms=999.0)  # error: no latency, counts as fail
    _record(m, cancelled=True, total_ms=999.0)  # cancel: no latency, not an error
    row = m.snapshot()["routes"][0]
    assert row["requests"] == 3
    assert row["errors"] == 1
    assert row["cancelled"] == 1
    assert row["success_rate"] == 2 / 3  # errors reduce success; cancel is not an error
    assert row["avg_total_ms"] == 100.0  # only the successful request contributes


def test_sheds_and_totals() -> None:
    m = RouteMetrics()
    _record(m, model="fast", backend="a", cost=1.0)
    _record(m, model="slow", backend="b", cost=2.0, error=True)
    m.record_shed("fast")
    m.record_shed("fast")
    snap = m.snapshot()
    assert snap["sheds"] == {"fast": 2}
    assert snap["totals"] == {
        "requests": 2,
        "errors": 1,
        "cancelled": 0,
        "cost": 3.0,
        "sheds": 2,
    }
    # Rows are keyed per (model, backend), sorted.
    assert [(r["model"], r["backend"]) for r in snap["routes"]] == [("fast", "a"), ("slow", "b")]


def test_empty_snapshot() -> None:
    snap = RouteMetrics().snapshot()
    assert snap["routes"] == []
    assert snap["totals"]["requests"] == 0


# -- Block 12.2a enrichment ------------------------------------------------


def test_queue_wait_average_over_all_requests() -> None:
    m = RouteMetrics()
    _record(m, queue_wait_ms=10.0, total_ms=100.0, completion_tokens=5)
    _record(m, queue_wait_ms=30.0, error=True)  # queue wait counts even for errors
    row = m.snapshot()["routes"][0]
    assert row["avg_queue_wait_ms"] == 20.0


def test_output_tps_only_valid_samples() -> None:
    m = RouteMetrics()
    _record(m, completion_tokens=10, total_ms=1000.0)  # 10 tok/s (valid)
    _record(m, completion_tokens=0, total_ms=1000.0)  # zero tokens: no sample
    _record(m, completion_tokens=5, total_ms=1000.0, cancelled=True)  # cancelled: no sample
    _record(m, completion_tokens=5, total_ms=1000.0, error=True)  # error: no sample
    _record(m, completion_tokens=5, total_ms=0.0)  # non-positive duration: no sample
    row = m.snapshot()["routes"][0]
    assert row["avg_output_tps"] == 10.0  # only the first request contributed


def test_reasons_fallbacks_policies_tier_and_attempts() -> None:
    m = RouteMetrics()
    _record(m, reason="model_map", policy="base", tier="primary", upstream_attempts=1)
    _record(
        m,
        reason="least_busy",
        fallbacks=("cascade_escalation", "spillover"),
        policy="cascade",
        tier="spillover",
        upstream_attempts=2,
    )
    row = m.snapshot()["routes"][0]
    assert row["reasons"] == {"least_busy": 1, "model_map": 1}
    assert row["fallbacks"] == {"cascade_escalation": 1, "spillover": 1}
    assert row["policies"] == {"base": 1, "cascade": 1}
    assert row["tier"] == "spillover"  # last-seen, stable per backend in practice
    assert row["upstream_attempts"] == 3


def test_old_fields_remain_compatible() -> None:
    # Recording without any new kwargs still works and old fields are unchanged.
    m = RouteMetrics()
    _record(m, prompt_tokens=100, completion_tokens=50, cost=0.15, total_ms=200.0, ttft_ms=40.0)
    row = m.snapshot()["routes"][0]
    assert row["avg_total_ms"] == 200.0 and row["avg_ttft_ms"] == 40.0
    assert row["avg_queue_wait_ms"] is None and row["avg_output_tps"] == 250.0
    assert row["reasons"] == {} and row["tier"] is None


def test_workload_rule_counts_are_safe_and_optional() -> None:
    m = RouteMetrics()
    _record(m, workload_rule="json-preferred")
    _record(m)
    assert m.snapshot()["routes"][0]["workload_rules"] == {"json-preferred": 1}
