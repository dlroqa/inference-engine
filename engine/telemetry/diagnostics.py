"""The redacted diagnostics bundle.

Produces a single JSON object an operator can attach to a support request:
redacted configuration, a hardware summary, a current metrics snapshot, and
recent structured log records. By construction it contains **no** raw prompts,
responses, or secrets:

- Configuration is filtered through a redaction pass that drops any field whose
  name looks secret-bearing and reports secret-bearing values as ``"***"``.
  (The engine stores API keys only as hashes and never keeps their plaintext, so
  there are no live secrets in ``Settings`` today; the redactor is defensive for
  fields added by later blocks.)
- Logs come from the structured ring buffer / ``log_events`` table, which only
  ever hold metadata.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any

from engine import __version__
from engine.config import Settings
from engine.inference.base import InferenceBackend
from engine.telemetry import hardware
from engine.telemetry.logbuffer import LogCollector

# Field-name fragments that mark a config value as potentially secret-bearing.
_SECRET_HINTS = ("secret", "token", "password", "passwd", "api_key", "apikey", "credential")


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def redact_config(settings: Settings) -> dict[str, Any]:
    """A JSON-safe, redacted view of the settings.

    Path values are stringified; secret-looking fields are masked. Nothing here is
    a live secret today, but the redactor keeps that true as config grows.
    """
    raw = settings.model_dump(mode="json")
    redacted: dict[str, Any] = {}
    for key, value in raw.items():
        if _looks_secret(key):
            redacted[key] = "***" if value is not None else None
        else:
            redacted[key] = value
    return redacted


def build_bundle(
    *,
    settings: Settings,
    backend: InferenceBackend | None,
    collector: LogCollector | None,
    snapshot: dict[str, Any] | None,
    log_limit: int = 200,
) -> dict[str, Any]:
    now = time.time()
    logs = collector.recent(limit=log_limit) if collector is not None else []
    return {
        "generated_at": _dt.datetime.fromtimestamp(now, tz=_dt.UTC).isoformat(),
        "engine_version": __version__,
        "config": redact_config(settings),
        "hardware": hardware.summary(),
        "metrics": snapshot,
        "recent_logs": logs,
        "notes": (
            "Redacted diagnostics: contains no prompts, responses, or secrets. "
            "Energy is reported as measured only where a validated power probe exists."
        ),
    }
