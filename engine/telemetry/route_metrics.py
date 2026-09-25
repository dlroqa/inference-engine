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

from dataclasses import dataclass, field


def output_tps_sample(
    *, completion_tokens: int, total_ms: float | None, error: bool, cancelled: bool
) -> float | None:
    """End-to-end output rate (tok/s, incl. TTFT) for a route sample, or None.

    A sample counts only for a successful, non-cancelled request that produced
    tokens over a positive duration (Block 12.2a). The single definition is shared
    by :meth:`RouteMetrics.record` and the serving ``route_decision`` log so the
    metric and the log never disagree.
    """
    if error or cancelled or completion_tokens <= 0 or not total_ms or total_ms <= 0:
        return None
    return completion_tokens / (total_ms / 1000.0)


@dataclass
class RouteStat:
    """Running totals for one (model, backend) route."""

    requests: int = 0
    errors: int = 0
    cancelled: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    #: Total upstream generation POSTs attributed to this route (Block 12.2a).
    upstream_attempts: int = 0
    #: Last-seen tier for this (model, backend) row (stable: a backend's tier
    #: does not change within a run).
    tier: str | None = None
    _total_ms_sum: float = 0.0
    _total_ms_n: int = 0
    _ttft_ms_sum: float = 0.0
    _ttft_ms_n: int = 0
    _queue_ms_sum: float = 0.0
    _queue_ms_n: int = 0
    _output_tps_sum: float = 0.0
    _output_tps_n: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    fallbacks: dict[str, int] = field(default_factory=dict)
    policies: dict[str, int] = field(default_factory=dict)
    workload_rules: dict[str, int] = field(default_factory=dict)

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

    @property
    def avg_queue_wait_ms(self) -> float | None:
        return self._queue_ms_sum / self._queue_ms_n if self._queue_ms_n else None

    @property
    def avg_output_tps(self) -> float | None:
        return self._output_tps_sum / self._output_tps_n if self._output_tps_n else None

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
            "avg_queue_wait_ms": self.avg_queue_wait_ms,
            "avg_output_tps": self.avg_output_tps,
            "upstream_attempts": self.upstream_attempts,
            "tier": self.tier,
            "reasons": dict(sorted(self.reasons.items())),
            "fallbacks": dict(sorted(self.fallbacks.items())),
            "policies": dict(sorted(self.policies.items())),
            "workload_rules": dict(sorted(self.workload_rules.items())),
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
        reason: str | None = None,
        fallbacks: tuple[str, ...] = (),
        policy: str | None = None,
        tier: str | None = None,
        queue_wait_ms: float | None = None,
        upstream_attempts: int = 0,
        workload_rule: str | None = None,
    ) -> None:
        stat = self._routes.setdefault((model, backend), RouteStat())
        stat.requests += 1
        stat.prompt_tokens += prompt_tokens
        stat.completion_tokens += completion_tokens
        stat.cost += cost
        stat.upstream_attempts += upstream_attempts
        if tier is not None:
            stat.tier = tier
        if reason is not None:
            stat.reasons[reason] = stat.reasons.get(reason, 0) + 1
        for fb in fallbacks:
            stat.fallbacks[fb] = stat.fallbacks.get(fb, 0) + 1
        if policy is not None:
            stat.policies[policy] = stat.policies.get(policy, 0) + 1
        if workload_rule is not None:
            stat.workload_rules[workload_rule] = stat.workload_rules.get(workload_rule, 0) + 1
        if error:
            stat.errors += 1
        if cancelled:
            stat.cancelled += 1
        # Queue wait is measured for every routed request (even errors/cancels).
        if queue_wait_ms is not None:
            stat._queue_ms_sum += queue_wait_ms
            stat._queue_ms_n += 1
        # Latency is only meaningful for a request that actually produced tokens.
        if not error and not cancelled:
            if total_ms is not None:
                stat._total_ms_sum += total_ms
                stat._total_ms_n += 1
            if ttft_ms is not None:
                stat._ttft_ms_sum += ttft_ms
                stat._ttft_ms_n += 1
        tps = output_tps_sample(
            completion_tokens=completion_tokens, total_ms=total_ms, error=error, cancelled=cancelled
        )
        if tps is not None:
            stat._output_tps_sum += tps
            stat._output_tps_n += 1

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
