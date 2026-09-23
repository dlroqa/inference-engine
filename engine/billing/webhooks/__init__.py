"""Outbound Standard Webhooks (Block 11.2).

Clients register endpoints; the engine signs and delivers events with retries,
backoff, a dead-letter state, a replayable delivery log, and secret rotation.

- :mod:`engine.billing.webhooks.signing` — Standard Webhooks signature scheme.
- :mod:`engine.billing.webhooks.store` — endpoints, rotatable secrets, delivery log/queue.
- :mod:`engine.billing.webhooks.delivery` — HTTP transport, retry/backoff, worker.
- :mod:`engine.billing.webhooks.dispatcher` — build an event envelope and fan it out.
"""

from __future__ import annotations

from engine.billing.webhooks.delivery import DeliveryWorker, Transport, TransportError
from engine.billing.webhooks.dispatcher import WebhookDispatcher
from engine.billing.webhooks.store import Delivery, Endpoint, WebhookStore

__all__ = [
    "Delivery",
    "DeliveryWorker",
    "Endpoint",
    "Transport",
    "TransportError",
    "WebhookDispatcher",
    "WebhookStore",
]
