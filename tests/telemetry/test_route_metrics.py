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
