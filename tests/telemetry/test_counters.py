"""Engine counters: active gauge, totals, error count, and rolling req/min."""

from __future__ import annotations

from engine.telemetry.counters import Counters


def test_active_and_totals() -> None:
    c = Counters(now=1000.0)
    c.request_started(now=1000.0)
    c.request_started(now=1000.0)
    snap = c.snapshot(now=1000.0)
    assert snap.requests_total == 2
    assert snap.requests_active == 2

    c.request_finished(prompt_tokens=10, completion_tokens=5)
    snap = c.snapshot(now=1000.0)
    assert snap.requests_active == 1
    assert snap.prompt_tokens_total == 10
    assert snap.completion_tokens_total == 5
    assert snap.requests_errors == 0


def test_error_counter() -> None:
    c = Counters(now=0.0)
    c.request_started(now=0.0)
    c.request_finished(prompt_tokens=0, completion_tokens=0, error=True)
    snap = c.snapshot(now=0.0)
    assert snap.requests_errors == 1
    assert snap.requests_active == 0


def test_active_never_goes_negative() -> None:
    c = Counters(now=0.0)
    # Finishing without a matching start must not drive the gauge below zero.
    c.request_finished(prompt_tokens=0, completion_tokens=0)
    assert c.snapshot(now=0.0).requests_active == 0


def test_requests_per_min_rolls_off() -> None:
    c = Counters(now=0.0)
    c.request_started(now=0.0)
    c.request_started(now=10.0)
    assert c.snapshot(now=30.0).requests_per_min == 2
    # 70s later the first two starts have aged out of the 60s window.
    assert c.snapshot(now=80.0).requests_per_min == 0


def test_uptime_tracks_start() -> None:
    c = Counters(now=100.0)
    assert c.snapshot(now=160.0).uptime_s == 60.0
