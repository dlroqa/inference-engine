"""Event dispatch: build a Standard Webhooks event envelope and fan it out to a
client's endpoints via the store. The background worker delivers them.

``emit`` never raises into its caller — a webhook problem must not break the
request or lifecycle action that produced the event. It returns the number of
deliveries enqueued (0 when webhooks are disabled or the client has no matching
endpoint).
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any

from engine.billing.webhooks.store import WebhookStore
from engine.logging_setup import get_logger

_log = get_logger("engine.webhooks")


class WebhookDispatcher:
    def __init__(self, store: WebhookStore, *, enabled: bool = True) -> None:
        self._store = store
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def emit(
        self,
        *,
        client_id: str,
        event_type: str,
        data: dict[str, Any],
        event_id: str | None = None,
        now: float | None = None,
    ) -> int:
        if not self._enabled:
            return 0
        eid = event_id or f"evt_{uuid.uuid4().hex}"
        envelope = {
            "id": eid,
            "type": event_type,
            "timestamp": _dt.datetime.now(tz=_dt.UTC).isoformat(),
            "data": data,
        }
        payload = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        try:
            return self._store.enqueue_event(
                client_id=client_id,
                event_id=eid,
                event_type=event_type,
                payload=payload,
                now=now,
            )
        except Exception:
            _log.exception(
                "webhook_emit_failed", extra={"event_type": event_type, "client_id": client_id}
            )
            return 0
