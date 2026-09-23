"""Block 11 — commercial controls: clients, plans, and the billing lifecycle.

The billing layer is provider-neutral at its core (:mod:`engine.billing.store`)
with a Stripe-specific adapter (:mod:`engine.billing.stripe`). Entitlements are
always operator-defined; webhook payloads never set quota/model limits directly.
"""

from __future__ import annotations

from engine.billing.store import (
    BillingStore,
    Client,
    Entitlement,
    KeyAccess,
    Plan,
    Subscription,
)

__all__ = [
    "BillingStore",
    "Client",
    "Entitlement",
    "KeyAccess",
    "Plan",
    "Subscription",
]
