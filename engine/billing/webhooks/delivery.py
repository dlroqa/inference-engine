"""Outbound delivery: signing headers, one HTTP attempt, retry/backoff, and the
background worker that drains the queue.

The HTTP transport is injectable (a stdlib-``urllib`` implementation by default, a
fake in tests), so the retry/backoff/dead-letter logic is deterministic and needs
no network. Attempts run in a worker thread (:func:`asyncio.to_thread`) so the
blocking POST never stalls the event loop.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol

from engine.billing.webhooks import signing
from engine.billing.webhooks.store import WebhookStore
from engine.logging_setup import get_logger

_log = get_logger("engine.webhooks")

DEFAULT_BACKOFF_SCHEDULE_S: tuple[float, ...] = (30.0, 120.0, 600.0, 1800.0)
DEFAULT_MAX_ATTEMPTS = 5


class TransportError(Exception):
    """A network-level failure (connection refused, timeout, DNS)."""


class Transport(Protocol):
    def post(self, url: str, body: str, headers: dict[str, str], timeout: float) -> int:
        """POST the body and return the HTTP status code. Raise
        :class:`TransportError` on a network-level failure."""
        ...


class UrllibTransport:
    """Default transport using the standard library (no third-party dependency)."""

    def post(self, url: str, body: str, headers: dict[str, str], timeout: float) -> int:
        req = urllib.request.Request(  # noqa: S310 - scheme is validated at registration
            url, data=body.encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            # A non-2xx response is a real HTTP status; the caller decides on retry.
            return int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(str(exc)) from exc


def _next_delay(attempts_after: int, schedule: tuple[float, ...]) -> float:
    idx = min(attempts_after - 1, len(schedule) - 1)
    return schedule[idx]


def build_headers(secrets: list[str], *, msg_id: str, timestamp: int, body: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signing.signature_header(secrets, msg_id, timestamp, body),
        "user-agent": "inference-engine-webhooks/1",
    }


class DeliveryWorker:
    """Drains due deliveries: sign, POST, and record success / retry / dead-letter."""

    def __init__(
        self,
        store: WebhookStore,
        *,
        transport: Transport | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_schedule_s: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE_S,
        delivery_timeout_s: float = 10.0,
        batch_limit: int = 50,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._transport: Transport = transport or UrllibTransport()
        self._max_attempts = max(1, max_attempts)
        self._schedule = backoff_schedule_s or DEFAULT_BACKOFF_SCHEDULE_S
        self._timeout = delivery_timeout_s
        self._batch_limit = batch_limit
        self._clock = clock or time.time

    def run_once(self) -> int:
        """Process every currently-due delivery once. Returns the number attempted.

        Synchronous and self-contained (used directly by tests); the async loop
        runs it in a worker thread.
        """
        now = self._clock()
        due = self._store.claim_due(now=now, limit=self._batch_limit)
        for delivery in due:
            self._attempt(delivery, now)
        return len(due)

    def _attempt(self, delivery: object, now: float) -> None:
        d = delivery  # typed as Delivery; kept loose to avoid a cyclic import here
        endpoint = self._store.get_endpoint(d.endpoint_id)  # type: ignore[attr-defined]
        if endpoint is None or endpoint.disabled:
            # Endpoint gone/disabled since enqueue: dead-letter without sending.
            self._store.mark_dead(
                d.id,  # type: ignore[attr-defined]
                status_code=None,
                error="endpoint missing or disabled",
                now=now,
            )
            return
        secrets = self._store.active_secrets(endpoint.id, now=now)
        headers = build_headers(
            secrets,
            msg_id=d.id,  # type: ignore[attr-defined]
            timestamp=int(now),
            body=d.payload,  # type: ignore[attr-defined]
        )
        attempts_after = d.attempts + 1  # type: ignore[attr-defined]
        try:
            status_code = self._transport.post(
                endpoint.url,
                d.payload,  # type: ignore[attr-defined]
                headers,
                self._timeout,
            )
        except TransportError as exc:
            self._on_failure(d, attempts_after, None, str(exc), now)
            return
        if 200 <= status_code < 300:
            self._store.mark_succeeded(d.id, status_code, now=now)  # type: ignore[attr-defined]
            return
        self._on_failure(d, attempts_after, status_code, f"HTTP {status_code}", now)

    def _on_failure(
        self, d: object, attempts_after: int, status_code: int | None, error: str, now: float
    ) -> None:
        if attempts_after >= self._max_attempts:
            self._store.mark_dead(
                d.id,  # type: ignore[attr-defined]
                status_code=status_code,
                error=error,
                now=now,
            )
            _log.warning(
                "webhook_delivery_dead",
                extra={"delivery_id": d.id, "attempts": attempts_after, "error": error},  # type: ignore[attr-defined]
            )
            return
        next_at = now + _next_delay(attempts_after, self._schedule)
        self._store.mark_retry(
            d.id,  # type: ignore[attr-defined]
            next_attempt_at=next_at,
            status_code=status_code,
            error=error,
            now=now,
        )

    async def run_loop(self, poll_interval_s: float) -> None:
        """Background loop: process due deliveries off-thread, then sleep."""
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:  # never let the loop die on a transient error
                _log.exception("webhook_worker_iteration_failed")
            await asyncio.sleep(poll_interval_s)
