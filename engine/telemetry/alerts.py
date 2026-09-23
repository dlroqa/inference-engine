"""Operator alerts (Block 11.6).

A small, read-only derivation of actionable operator alerts from signals the
engine already records — suspended/canceled clients, clients that crossed a usage
threshold, dead-lettered webhook deliveries, and unhealthy backends. Nothing here
is persisted or pushed; the monitoring view fetches it and each alert carries a
cross-link target so the UI can jump to the relevant surface.

:func:`compute_alerts` is pure (it takes already-fetched inputs) so it is trivial
to test without a running app.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


@dataclass(slots=True)
class Alert:
    severity: str
    kind: str
    message: str
    target_type: str | None = None  # "client" | "backend" | "webhook_deliveries" | None
    target_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "message": self.message,
            "target_type": self.target_type,
            "target_id": self.target_id,
        }


def compute_alerts(
    *,
    clients: Sequence[object],
    threshold_events: Sequence[object],
    dead_delivery_count: int,
    unhealthy_backends: Sequence[str],
) -> list[Alert]:
    """Build the operator alert list, most severe first.

    - ``clients`` — billing clients; those ``suspended``/``canceled`` alert.
    - ``threshold_events`` — recent ``usage.threshold.reached`` events (deduped to
      the most recent per client).
    - ``dead_delivery_count`` — webhook deliveries in the dead-letter state.
    - ``unhealthy_backends`` — names of backends that are loaded but not ready.
    """
    alerts: list[Alert] = []

    for backend_name in unhealthy_backends:
        alerts.append(
            Alert(
                severity=SEVERITY_CRITICAL,
                kind="backend_unhealthy",
                message=f"Backend {backend_name!r} is loaded but not ready",
                target_type="backend",
                target_id=backend_name,
            )
        )

    for client in clients:
        status = getattr(client, "status", None)
        cid = getattr(client, "id", None)
        if status == "canceled":
            alerts.append(
                Alert(
                    severity=SEVERITY_CRITICAL,
                    kind="client_canceled",
                    message=f"Client {cid} is canceled; its keys are revoked",
                    target_type="client",
                    target_id=cid,
                )
            )
        elif status == "suspended":
            alerts.append(
                Alert(
                    severity=SEVERITY_WARNING,
                    kind="client_suspended",
                    message=f"Client {cid} is suspended",
                    target_type="client",
                    target_id=cid,
                )
            )

    if dead_delivery_count > 0:
        alerts.append(
            Alert(
                severity=SEVERITY_WARNING,
                kind="webhook_dead_letters",
                message=f"{dead_delivery_count} webhook deliver"
                f"{'y is' if dead_delivery_count == 1 else 'ies are'} dead-lettered",
                target_type="webhook_deliveries",
                target_id=None,
            )
        )

    seen_clients: set[str] = set()
    for event in threshold_events:
        cid = getattr(event, "client_id", None)
        if cid is None or cid in seen_clients:
            continue
        seen_clients.add(cid)
        alerts.append(
            Alert(
                severity=SEVERITY_WARNING,
                kind="usage_threshold",
                message=f"Client {cid} crossed its usage threshold",
                target_type="client",
                target_id=cid,
            )
        )

    order = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    alerts.sort(key=lambda a: order.get(a.severity, 3))
    return alerts
