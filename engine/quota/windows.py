"""Time-window helpers for quota accounting.

- 5-hour window: **rolling** — the last 5 hours from now, continuously.
- Weekly window: **fixed** — anchored to Monday 00:00 UTC; does not move with usage.
"""

from __future__ import annotations

import datetime as _dt

WINDOW_5H_SECONDS = 5 * 60 * 60


def rolling_5h_start(now: float) -> float:
    """Epoch seconds marking the start of the rolling 5-hour window."""
    return now - WINDOW_5H_SECONDS


def week_start(now: float) -> float:
    """Epoch seconds for the start of the current week (Monday 00:00 UTC)."""
    dt = _dt.datetime.fromtimestamp(now, tz=_dt.UTC)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - _dt.timedelta(days=dt.weekday())
    return monday.timestamp()


def week_reset(now: float) -> float:
    """Epoch seconds when the current weekly window resets."""
    return week_start(now) + 7 * 24 * 60 * 60
