"""Usage store: records attribution rows and sums CU over quota windows.

The ``usage_events`` table is the single source of truth for both attribution and
the CU windows (summed over time ranges), so counters can never disagree with the
audit log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from engine.quota.windows import rolling_5h_start, week_reset, week_start
from engine.store.db import connect


@dataclass(slots=True)
class WindowUsage:
    used: float
    limit: float  # 0 == unlimited
    reset_at: float

    @property
    def remaining(self) -> float:
        if self.limit <= 0:
            return float("inf")
        return max(0.0, self.limit - self.used)

    def would_exceed(self, additional: float) -> bool:
        if self.limit <= 0:
            return False
        return self.used + additional > self.limit


class UsageStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def record(
        self,
        *,
        key_id: str | None,
        request_id: str,
        endpoint: str,
        model: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        cu: float,
        status: int | None,
        ts: float | None = None,
    ) -> None:
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO usage_events "
                    "(ts, key_id, request_id, endpoint, model, prompt_tokens, "
                    " completion_tokens, cu, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (
                        ts if ts is not None else time.time(),
                        key_id,
                        request_id,
                        endpoint,
                        model,
                        prompt_tokens,
                        completion_tokens,
                        cu,
                        status,
                    ),
                )
        finally:
            conn.close()

    def _sum_cu(self, key_id: str, since: float) -> float:
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cu), 0.0) FROM usage_events WHERE key_id = ? AND ts >= ?;",
                (key_id, since),
            ).fetchone()
            return float(row[0])
        finally:
            conn.close()

    def usage_5h(self, key_id: str, limit: float, now: float | None = None) -> WindowUsage:
        now = now if now is not None else time.time()
        used = self._sum_cu(key_id, rolling_5h_start(now))
        # The rolling window "resets" as the oldest usage ages out; report a
        # conservative full-window horizon.
        return WindowUsage(used=used, limit=limit, reset_at=now + 5 * 60 * 60)

    def usage_weekly(self, key_id: str, limit: float, now: float | None = None) -> WindowUsage:
        now = now if now is not None else time.time()
        used = self._sum_cu(key_id, week_start(now))
        return WindowUsage(used=used, limit=limit, reset_at=week_reset(now))
