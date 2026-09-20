"""Operability core (Block 4).

The telemetry package gives an operator enough evidence to understand engine
health, load, and individual request failures without leaking secrets:

- :mod:`engine.telemetry.events` — a bounded in-memory async event bus with a
  recent-event ring buffer, backing the ``/ws/metrics`` and ``/ws/feed`` streams.
- :mod:`engine.telemetry.counters` — cumulative + rolling request/token counters.
- :mod:`engine.telemetry.resources` — process/system resource sampling (psutil).
- :mod:`engine.telemetry.power` — energy measurement with an explicit
  *measured*-vs-*unavailable* state (never TDP-derived estimates presented as
  measured).
- :mod:`engine.telemetry.hardware` — a static hardware summary.
- :mod:`engine.telemetry.sampler` — the background metrics sampler.
- :mod:`engine.telemetry.logbuffer` — a bounded mirror of recent structured logs
  (in-memory ring + ``log_events`` table) that also feeds the event bus.
- :mod:`engine.telemetry.taxonomy` — the structured error taxonomy.
- :mod:`engine.telemetry.diagnostics` — the redacted diagnostics bundle.
"""

from __future__ import annotations

from engine.telemetry.counters import Counters
from engine.telemetry.events import Event, EventBus, EventChannel, EventType
from engine.telemetry.taxonomy import ErrorCategory, classify

__all__ = [
    "Counters",
    "ErrorCategory",
    "Event",
    "EventBus",
    "EventChannel",
    "EventType",
    "classify",
]
