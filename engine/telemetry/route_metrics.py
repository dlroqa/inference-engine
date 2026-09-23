"""Per-route cost + performance measurement (Block 10, sub-slice 5b).

Accumulates honest, *measurable* signals for each ``(virtual-model, backend)``
pair a request is attributed to: request/error/cancel counts, tokens, token cost,
and latency (average total + time-to-first-token), plus per-model admission sheds.
This is measurement only — it does not change routing decisions (that would be a
later, adaptive refinement) — and it deliberately does not attempt a "quality"
score, which needs evaluation harnesses (Block 12).

Updates are synchronous dict mutations made from the event loop (no awaits mid
update), so no locking is needed for the single-process engine.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RouteStat:
    """Running totals for one (model, backend) route."""

    requests: int = 0
    errors: int = 0
    cancelled: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    _total_ms_sum: float = 0.0
    _total_ms_n: int = 0
    _ttft_ms_sum: float = 0.0
    _ttft_ms_n: int = 0

    @property
    def success_rate(self) -> float | None:
        if self.requests == 0:
            return None
        return (self.requests - self.errors) / self.requests

    @property
    def avg_total_ms(self) -> float | None:
        return self._total_ms_sum / self._total_ms_n if self._total_ms_n else None

    @property
    def avg_ttft_ms(self) -> float | None:
        return self._ttft_ms_sum / self._ttft_ms_n if self._ttft_ms_n else None

    def as_dict(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "cancelled": self.cancelled,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost": round(self.cost, 6),
            "success_rate": self.success_rate,
            "avg_total_ms": self.avg_total_ms,
            "avg_ttft_ms": self.avg_ttft_ms,
        }


class RouteMetrics:
    """Cost + performance accumulator keyed by ``(model, backend)``."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], RouteStat] = {}
        self._sheds: dict[str, int] = {}

    def record(
        self,
        *,
        model: str,
        backend: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        total_ms: float | None,
        ttft_ms: float | None,
        error: bool,
        cancelled: bool,
    ) -> None:
        stat = self._routes.setdefault((model, backend), RouteStat())
        stat.requests += 1
        stat.prompt_tokens += prompt_tokens
        stat.completion_tokens += completion_tokens
        stat.cost += cost
        if error:
            stat.errors += 1
        if cancelled:
            stat.cancelled += 1
        # Latency is only meaningful for a request that actually produced tokens.
        if not error and not cancelled:
            if total_ms is not None:
                stat._total_ms_sum += total_ms
                stat._total_ms_n += 1
            if ttft_ms is not None:
                stat._ttft_ms_sum += ttft_ms
                stat._ttft_ms_n += 1

    def record_shed(self, model: str) -> None:
        """A request that was never admitted (saturation / no backend available)."""
        self._sheds[model] = self._sheds.get(model, 0) + 1

    def snapshot(self) -> dict[str, object]:
        routes = [
            {"model": model, "backend": backend, **stat.as_dict()}
            for (model, backend), stat in sorted(self._routes.items())
        ]
        totals = {
            "requests": sum(s.requests for s in self._routes.values()),
            "errors": sum(s.errors for s in self._routes.values()),
            "cancelled": sum(s.cancelled for s in self._routes.values()),
            "cost": round(sum(s.cost for s in self._routes.values()), 6),
            "sheds": sum(self._sheds.values()),
        }
        return {
            "routes": routes,
            "sheds": dict(sorted(self._sheds.items())),
            "totals": totals,
        }
