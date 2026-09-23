"""Billing store: clients, plans, subscriptions, entitlement resolution, and the
inbound-webhook idempotency ledger.

Design rules that this module enforces:

- **Entitlements are operator-defined.** Quota and model limits come only from
  the ``plans`` table, which operators manage. Webhook payloads select a plan by
  id but never define its limits, so a spoofed or malformed event can never
  widen entitlements.
- **usage_events stays the metering source of truth.** This store never records
  usage; it only *resolves the limits* the gateway applies against the existing
  usage windows, and offers a reconciliation read that sums the same rows.
- **Key access is derived, not duplicated.** A key's effective status is computed
  from its own status and its owning client's status at request time, so a single
  lifecycle action on a client governs all of its keys.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path

from engine.store.db import connect

# Effective per-key access states the gateway acts on.
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_REVOKED = "revoked"
STATUS_CANCELED = "canceled"


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat()


@dataclass(slots=True)
class Plan:
    id: str
    name: str
    quota_5h_cu: float
    quota_weekly_cu: float
    rate_limit_per_min: int | None
    allowed_models: tuple[str, ...] | None  # None == all models


@dataclass(slots=True)
class Client:
    id: str
    external_ref: str | None
    email: str | None
    status: str


@dataclass(slots=True)
class Subscription:
    id: str
    client_id: str
    plan_id: str | None
    provider: str
    provider_sub_id: str
    status: str
    current_period_end: str | None


@dataclass(slots=True)
class Entitlement:
    """The limits a request is authorized against, resolved from a client's plan."""

    plan_id: str
    quota_5h_cu: float
    quota_weekly_cu: float
    rate_limit_per_min: int | None  # None == use the engine-wide default
    allowed_models: tuple[str, ...] | None  # None == all models


@dataclass(slots=True)
class KeyAccess:
    """Effective access for one key: its enforcement status and, when active and
    owned, the plan entitlement to apply. ``entitlement is None`` means "fall back
    to the engine-wide limits" (an unowned/legacy key, or no plan resolved)."""

    status: str
    entitlement: Entitlement | None


def _plan_from_row(row: object) -> Plan:
    models_raw = row["allowed_models"]  # type: ignore[index]
    allowed = tuple(json.loads(models_raw)) if models_raw else None
    return Plan(
        id=row["id"],  # type: ignore[index]
        name=row["name"],  # type: ignore[index]
        quota_5h_cu=float(row["quota_5h_cu"]),  # type: ignore[index]
        quota_weekly_cu=float(row["quota_weekly_cu"]),  # type: ignore[index]
        rate_limit_per_min=row["rate_limit_per_min"],  # type: ignore[index]
        allowed_models=allowed,
    )


class BillingStore:
    """SQLite-backed billing store (short-lived connections per operation)."""

    def __init__(self, db_path: Path, *, default_plan_id: str | None = None) -> None:
        self._db_path = db_path
        self._default_plan_id = default_plan_id

    # ---- plans -----------------------------------------------------------

    def upsert_plan(
        self,
        *,
        id: str,
        name: str,
        quota_5h_cu: float = 0.0,
        quota_weekly_cu: float = 0.0,
        rate_limit_per_min: int | None = None,
        allowed_models: list[str] | None = None,
    ) -> Plan:
        models_json = json.dumps(list(allowed_models)) if allowed_models is not None else None
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO plans "
                    "(id, name, quota_5h_cu, quota_weekly_cu, rate_limit_per_min, "
                    " allowed_models, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    " quota_5h_cu=excluded.quota_5h_cu, "
                    " quota_weekly_cu=excluded.quota_weekly_cu, "
                    " rate_limit_per_min=excluded.rate_limit_per_min, "
                    " allowed_models=excluded.allowed_models;",
                    (
                        id,
                        name,
                        quota_5h_cu,
                        quota_weekly_cu,
                        rate_limit_per_min,
                        models_json,
                        _now_iso(),
                    ),
                )
        finally:
            conn.close()
        return Plan(
            id=id,
            name=name,
            quota_5h_cu=quota_5h_cu,
            quota_weekly_cu=quota_weekly_cu,
            rate_limit_per_min=rate_limit_per_min,
            allowed_models=tuple(allowed_models) if allowed_models is not None else None,
        )

    def get_plan(self, plan_id: str) -> Plan | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM plans WHERE id = ?;", (plan_id,)).fetchone()
            return _plan_from_row(row) if row is not None else None
        finally:
            conn.close()

    def list_plans(self) -> list[Plan]:
        conn = connect(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM plans ORDER BY id;").fetchall()
            return [_plan_from_row(r) for r in rows]
        finally:
            conn.close()

    # ---- clients ---------------------------------------------------------

    def create_client(
        self, *, id: str, external_ref: str | None = None, email: str | None = None
    ) -> Client:
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO clients (id, external_ref, email, status, created_at) "
                    "VALUES (?, ?, ?, 'active', ?);",
                    (id, external_ref, email, _now_iso()),
                )
        finally:
            conn.close()
        return Client(id=id, external_ref=external_ref, email=email, status=STATUS_ACTIVE)

    def get_client(self, client_id: str) -> Client | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM clients WHERE id = ?;", (client_id,)).fetchone()
            return self._client_from_row(row) if row is not None else None
        finally:
            conn.close()

    def get_client_by_external_ref(self, external_ref: str) -> Client | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM clients WHERE external_ref = ?;", (external_ref,)
            ).fetchone()
            return self._client_from_row(row) if row is not None else None
        finally:
            conn.close()

    def list_clients(self) -> list[Client]:
        conn = connect(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM clients ORDER BY created_at;").fetchall()
            return [self._client_from_row(r) for r in rows]
        finally:
            conn.close()

    def set_client_status(self, client_id: str, status: str) -> bool:
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE clients SET status = ? WHERE id = ?;", (status, client_id)
                )
            return cur.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def _client_from_row(row: object) -> Client:
        return Client(
            id=row["id"],  # type: ignore[index]
            external_ref=row["external_ref"],  # type: ignore[index]
            email=row["email"],  # type: ignore[index]
            status=row["status"],  # type: ignore[index]
        )

    # ---- keys <-> clients ------------------------------------------------

    def attach_key(self, key_id: str, client_id: str) -> bool:
        """Assign a key to a client. Returns True if the key exists."""
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE api_keys SET client_id = ? WHERE id = ?;", (client_id, key_id)
                )
            return cur.rowcount > 0
        finally:
            conn.close()

    def client_id_for_key(self, key_id: str) -> str | None:
        """Return the client id that owns a key, or None if unowned/unknown."""
        conn = connect(self._db_path)
        try:
            row = conn.execute("SELECT client_id FROM api_keys WHERE id = ?;", (key_id,)).fetchone()
            return row["client_id"] if row is not None else None
        finally:
            conn.close()

    def revoke_client_keys(self, client_id: str) -> int:
        """Terminally revoke every live key owned by a client.

        Sets both ``status='revoked'`` and ``revoked_at`` so the Block 3
        ``KeyStore.verify`` path (which checks ``revoked_at``) also rejects them.
        Returns the number of keys revoked.
        """
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE api_keys SET status = 'revoked', revoked_at = ? "
                    "WHERE client_id = ? AND revoked_at IS NULL;",
                    (_now_iso(), client_id),
                )
            return cur.rowcount
        finally:
            conn.close()

    # ---- subscriptions ---------------------------------------------------

    def upsert_subscription(
        self,
        *,
        id: str,
        client_id: str,
        plan_id: str | None,
        provider: str,
        provider_sub_id: str,
        status: str,
        current_period_end: str | None = None,
    ) -> None:
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO subscriptions "
                    "(id, client_id, plan_id, provider, provider_sub_id, status, "
                    " current_period_end, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(provider, provider_sub_id) DO UPDATE SET "
                    " plan_id=excluded.plan_id, status=excluded.status, "
                    " current_period_end=excluded.current_period_end, "
                    " updated_at=excluded.updated_at;",
                    (
                        id,
                        client_id,
                        plan_id,
                        provider,
                        provider_sub_id,
                        status,
                        current_period_end,
                        _now_iso(),
                    ),
                )
        finally:
            conn.close()

    def get_subscription_by_provider(
        self, provider: str, provider_sub_id: str
    ) -> Subscription | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE provider = ? AND provider_sub_id = ?;",
                (provider, provider_sub_id),
            ).fetchone()
            return self._sub_from_row(row) if row is not None else None
        finally:
            conn.close()

    @staticmethod
    def _sub_from_row(row: object) -> Subscription:
        return Subscription(
            id=row["id"],  # type: ignore[index]
            client_id=row["client_id"],  # type: ignore[index]
            plan_id=row["plan_id"],  # type: ignore[index]
            provider=row["provider"],  # type: ignore[index]
            provider_sub_id=row["provider_sub_id"],  # type: ignore[index]
            status=row["status"],  # type: ignore[index]
            current_period_end=row["current_period_end"],  # type: ignore[index]
        )

    # ---- entitlement resolution -----------------------------------------

    def key_access(self, key_id: str) -> KeyAccess:
        """Resolve a key's effective status and plan entitlement.

        - A revoked key -> ``revoked`` (terminal).
        - A key whose owning client is suspended/canceled -> ``suspended``.
        - An unowned key -> ``active`` with no entitlement (engine-wide limits).
        - Otherwise -> ``active`` with the client's resolved plan entitlement.
        """
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT k.status AS key_status, k.client_id AS client_id, "
                "       c.status AS client_status "
                "FROM api_keys k LEFT JOIN clients c ON k.client_id = c.id "
                "WHERE k.id = ?;",
                (key_id,),
            ).fetchone()
            if row is None:
                # Unknown key id (e.g. the loopback "local" attribution): no billing.
                return KeyAccess(status=STATUS_ACTIVE, entitlement=None)

            key_status = row["key_status"]
            if key_status == STATUS_REVOKED:
                return KeyAccess(status=STATUS_REVOKED, entitlement=None)

            client_id = row["client_id"]
            if client_id is None:
                # Unowned/legacy key: honor a per-key suspension, else engine-wide.
                effective = STATUS_SUSPENDED if key_status == STATUS_SUSPENDED else STATUS_ACTIVE
                return KeyAccess(status=effective, entitlement=None)

            if row["client_status"] in (STATUS_SUSPENDED, STATUS_CANCELED):
                return KeyAccess(status=STATUS_SUSPENDED, entitlement=None)
            if key_status == STATUS_SUSPENDED:
                return KeyAccess(status=STATUS_SUSPENDED, entitlement=None)

            return KeyAccess(
                status=STATUS_ACTIVE,
                entitlement=self._entitlement_for_client(conn, client_id),
            )
        finally:
            conn.close()

    def _entitlement_for_client(self, conn: object, client_id: str) -> Entitlement | None:
        # Prefer an active subscription's plan; fall back to the default plan.
        row = conn.execute(  # type: ignore[attr-defined]
            "SELECT plan_id FROM subscriptions "
            "WHERE client_id = ? AND status = 'active' "
            "ORDER BY updated_at DESC LIMIT 1;",
            (client_id,),
        ).fetchone()
        plan_id = row["plan_id"] if row is not None else None
        if plan_id is None:
            plan_id = self._default_plan_id
        if plan_id is None:
            return None
        plan_row = conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM plans WHERE id = ?;", (plan_id,)
        ).fetchone()
        if plan_row is None:
            return None
        plan = _plan_from_row(plan_row)
        return Entitlement(
            plan_id=plan.id,
            quota_5h_cu=plan.quota_5h_cu,
            quota_weekly_cu=plan.quota_weekly_cu,
            rate_limit_per_min=plan.rate_limit_per_min,
            allowed_models=plan.allowed_models,
        )

    # ---- inbound-webhook idempotency ------------------------------------

    def record_event_once(self, provider: str, event_id: str, event_type: str | None) -> bool:
        """Record an inbound event id. Returns True if newly recorded, False if a
        duplicate (already present)."""
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO webhook_events "
                    "(provider, event_id, event_type, received_at) VALUES (?, ?, ?, ?);",
                    (provider, event_id, event_type, _now_iso()),
                )
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_event_processed(self, provider: str, event_id: str, result: str) -> None:
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE webhook_events SET processed_at = ?, result = ? "
                    "WHERE provider = ? AND event_id = ?;",
                    (_now_iso(), result, provider, event_id),
                )
        finally:
            conn.close()

    def forget_event(self, provider: str, event_id: str) -> None:
        """Remove a recorded event id so it can be retried.

        Used when processing raised before completing, so a provider redelivery
        is handled instead of being dropped as a duplicate.
        """
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM webhook_events WHERE provider = ? AND event_id = ?;",
                    (provider, event_id),
                )
        finally:
            conn.close()

    # ---- metering reconciliation ----------------------------------------

    def client_usage_cu(self, client_id: str, since: float) -> float:
        """Sum CU across all of a client's keys from ``usage_events`` since a time.

        This reads the same rows the quota windows sum, so a client's enforced
        usage always reconciles with the attribution log.
        """
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(u.cu), 0.0) FROM usage_events u "
                "JOIN api_keys k ON u.key_id = k.id "
                "WHERE k.client_id = ? AND u.ts >= ?;",
                (client_id, since),
            ).fetchone()
            return float(row[0])
        finally:
            conn.close()
