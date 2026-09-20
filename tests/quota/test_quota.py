"""Compute model, windows, and usage-store sums."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from engine.quota.compute import ComputeModel, estimate_prompt_tokens
from engine.quota.store import UsageStore
from engine.quota.windows import WINDOW_5H_SECONDS, rolling_5h_start, week_reset, week_start
from engine.store.db import connect
from engine.store.migrations import apply_migrations


def test_compute_model() -> None:
    m = ComputeModel(prompt_weight=1.0, completion_weight=2.0)
    assert m.actual(10, 5) == 10 + 10
    assert m.estimate(10, 8) == 10 + 16
    assert m.actual(10, 5, multiplier=2.0) == 40


def test_estimate_prompt_tokens() -> None:
    assert estimate_prompt_tokens("") == 1
    assert estimate_prompt_tokens("abcd") == 1
    assert estimate_prompt_tokens("a" * 40) == 10


def test_window_helpers() -> None:
    now = _dt.datetime(2026, 9, 16, 12, 0, tzinfo=_dt.UTC).timestamp()  # a Wednesday
    assert rolling_5h_start(now) == now - WINDOW_5H_SECONDS
    ws = week_start(now)
    # Monday 00:00 UTC of that week is 2026-09-14.
    assert _dt.datetime.fromtimestamp(ws, tz=_dt.UTC) == _dt.datetime(
        2026, 9, 14, 0, 0, tzinfo=_dt.UTC
    )
    assert week_reset(now) == ws + 7 * 24 * 3600


def _usage(tmp_path: Path) -> UsageStore:
    db = tmp_path / "u.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return UsageStore(db)


def test_usage_sums_respect_windows(tmp_path: Path) -> None:
    store = _usage(tmp_path)
    now = 1_000_000.0
    # Inside the 5h window.
    store.record(
        key_id="k1",
        request_id="r1",
        endpoint="/v1/chat/completions",
        model="m",
        prompt_tokens=10,
        completion_tokens=5,
        cu=15.0,
        status=200,
        ts=now - 100,
    )
    # Outside the 5h window but same week.
    store.record(
        key_id="k1",
        request_id="r2",
        endpoint="/v1/chat/completions",
        model="m",
        prompt_tokens=10,
        completion_tokens=5,
        cu=100.0,
        status=200,
        ts=now - WINDOW_5H_SECONDS - 100,
    )
    u5h = store.usage_5h("k1", limit=1000.0, now=now)
    assert u5h.used == 15.0
    assert u5h.remaining == 985.0

    uweek = store.usage_weekly("k1", limit=1000.0, now=now)
    assert uweek.used == 115.0  # both rows are within the current week


def test_window_would_exceed_and_unlimited(tmp_path: Path) -> None:
    store = _usage(tmp_path)
    now = 2_000_000.0
    store.record(
        key_id="k",
        request_id="r",
        endpoint="e",
        model="m",
        prompt_tokens=0,
        completion_tokens=0,
        cu=90.0,
        status=200,
        ts=now - 10,
    )
    u = store.usage_5h("k", limit=100.0, now=now)
    assert u.would_exceed(20.0) is True
    assert u.would_exceed(5.0) is False

    unlimited = store.usage_5h("k", limit=0.0, now=now)
    assert unlimited.would_exceed(1e9) is False
    assert unlimited.remaining == float("inf")
