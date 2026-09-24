"""Pure aggregation of client-side benchmark samples (Block 12.2b).

Turns per-request :class:`RequestSample`s into the RFC §8 metric set, per workload
slice and overall. Everything here is pure and JSON-serialisable; no clock,
network, or engine state is touched.

Definitions (kept deliberately explicit — see ``docs/benchmarks.md``):

* **TTFT** is client-side time to the first non-empty ``choices[].delta.content``
  (the driver records it; role-only opening frames do not count).
* **Percentiles** use nearest-rank over only the samples for which the metric is
  defined; an empty/undefined set is ``None``, never ``0``.
* **Denominators**: ``attempted`` = all samples; ``admitted`` = attempted − shed;
  ``accepted_rate`` = admitted / wall-second; ``shed_rate`` = shed / attempted;
  ``error_rate`` and pre/post-stream failure rates = count / admitted.
  Cancellations are reported separately and are never errors or sheds.
* **Output TPS / token / cost** are **not** computed from client SSE content
  chunks (chunks are not tokenizer tokens, and the public SSE exposes no terminal
  usage). They come only from an aggregate ``/admin/routes`` before/after delta
  under an exclusive-target run — carried separately in the report, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

Outcome = str  # "ok" | "error" | "cancelled"


@dataclass(frozen=True)
class RequestSample:
    """Safe per-request measurement — no prompt or response content is stored."""

    slice: str
    model: str
    ttft_ms: float | None
    total_ms: float | None
    content_chunks: int
    outcome: Outcome
    observed_first_content: bool
    failure_phase: str | None  # "pre_stream" | "post_stream" | None
    shed: bool


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (matches ``scripts/load_test.py::_pct``), or None.

    Empty -> ``None``; a singleton -> that value for any ``q``; ``q`` is clamped to
    ``[0, 100]`` and the index to ``[0, len-1]``.
    """
    if not values:
        return None
    ordered = sorted(values)
    q = min(100.0, max(0.0, q))
    idx = min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


def _summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p50": None, "p95": None, "p99": None}
    return {
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _slice_metrics(samples: list[RequestSample]) -> dict[str, object]:
    attempted = len(samples)
    shed = sum(1 for s in samples if s.shed)
    admitted = attempted - shed
    # A shed (429) request was never admitted, so it is neither an error nor a
    # failure — it is counted only by the shed rate (shed / attempted).
    errors = sum(1 for s in samples if s.outcome == "error" and not s.shed)
    cancelled = sum(1 for s in samples if s.outcome == "cancelled" and not s.shed)
    pre = sum(1 for s in samples if s.failure_phase == "pre_stream" and not s.shed)
    post = sum(1 for s in samples if s.failure_phase == "post_stream" and not s.shed)
    ttft = [s.ttft_ms for s in samples if s.ttft_ms is not None]
    # Latency percentiles use successful, token-producing requests only.
    total = [s.total_ms for s in samples if s.outcome == "ok" and s.total_ms is not None]
    return {
        "attempted": attempted,
        "admitted": admitted,
        "shed": shed,
        "errors": errors,
        "cancelled": cancelled,
        "pre_stream_failures": pre,
        "post_stream_failures": post,
        "shed_rate": _rate(shed, attempted),
        "error_rate": _rate(errors, admitted),
        "pre_stream_rate": _rate(pre, admitted),
        "post_stream_rate": _rate(post, admitted),
        "ttft_ms": _summary(ttft),
        "total_ms": _summary(total),
    }


def aggregate(samples: list[RequestSample], *, wall_s: float) -> dict[str, object]:
    """Aggregate samples into per-slice + overall client metrics (pure)."""
    by_slice: dict[str, list[RequestSample]] = {}
    for s in samples:
        by_slice.setdefault(s.slice, []).append(s)
    slices = {name: _slice_metrics(group) for name, group in sorted(by_slice.items())}
    overall = _slice_metrics(samples)
    admitted = sum(1 for s in samples if not s.shed)
    overall["accepted_rate"] = (admitted / wall_s) if wall_s > 0 else None
    return {"wall_s": wall_s, "slices": slices, "overall": overall}
