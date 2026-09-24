"""Pure metric aggregation: percentiles, denominators, classification (Block 12.2b)."""

from __future__ import annotations

from engine.bench.metrics import RequestSample, aggregate, percentile


def _s(
    slice_: str = "unique",
    *,
    ttft: float | None = 10.0,
    total: float | None = 100.0,
    chunks: int = 5,
    outcome: str = "ok",
    first: bool = True,
    failure: str | None = None,
    shed: bool = False,
) -> RequestSample:
    return RequestSample(slice_, "m", ttft, total, chunks, outcome, first, failure, shed)


def test_percentile_boundaries_and_unavailable() -> None:
    assert percentile([], 50) is None
    assert percentile([42.0], 0) == 42.0
    assert percentile([42.0], 100) == 42.0
    vals = [float(x) for x in range(1, 101)]  # 1..100
    assert percentile(vals, 50) in (50.0, 51.0)
    assert percentile(vals, 99) in (99.0, 100.0)


def test_denominators_shed_error_cancel() -> None:
    samples = [
        _s(outcome="ok"),
        _s(outcome="ok"),
        _s(outcome="error", failure="pre_stream", first=False, ttft=None),
        _s(outcome="cancelled", first=True),
        _s(outcome="error", ttft=None, total=5.0, shed=True),  # 429 shed
    ]
    res = aggregate(samples, wall_s=2.0)
    o = res["overall"]
    assert o["attempted"] == 5
    assert o["shed"] == 1
    assert o["admitted"] == 4  # attempted - shed
    assert o["errors"] == 1  # shed 429 is NOT an error
    assert o["cancelled"] == 1
    assert o["shed_rate"] == 1 / 5
    assert o["error_rate"] == 1 / 4  # over admitted
    assert o["accepted_rate"] == 4 / 2.0


def test_ttft_and_latency_sample_selection() -> None:
    samples = [
        _s(ttft=10.0, total=100.0, outcome="ok"),
        _s(ttft=20.0, total=200.0, outcome="ok"),
        _s(ttft=None, total=999.0, outcome="error", first=False, failure="pre_stream"),
        _s(ttft=30.0, total=300.0, outcome="cancelled"),  # ttft counts, latency does not
    ]
    o = aggregate(samples, wall_s=1.0)["overall"]
    # TTFT over samples with observed first content (3 samples: 10, 20, 30).
    assert o["ttft_ms"]["p50"] in (20.0,)
    # Latency percentiles use successful requests only (100, 200).
    assert o["total_ms"]["mean"] == 150.0


def test_pre_post_stream_classification() -> None:
    samples = [
        _s(outcome="error", first=False, ttft=None, failure="pre_stream"),
        _s(outcome="error", first=True, failure="post_stream"),
    ]
    o = aggregate(samples, wall_s=1.0)["overall"]
    assert o["pre_stream_failures"] == 1
    assert o["post_stream_failures"] == 1
    assert o["pre_stream_rate"] == 1 / 2
    assert o["post_stream_rate"] == 1 / 2


def test_empty_metrics_are_none_not_zero() -> None:
    o = aggregate([_s(outcome="error", ttft=None, total=None, first=False)], wall_s=1.0)["overall"]
    assert o["ttft_ms"]["p95"] is None  # no defined samples
    assert o["total_ms"]["p95"] is None
