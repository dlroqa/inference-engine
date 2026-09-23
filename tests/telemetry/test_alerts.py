"""Operator alert derivation (Block 11.6a)."""

from __future__ import annotations

from dataclasses import dataclass

from engine.telemetry.alerts import compute_alerts


@dataclass
class _Client:
    id: str
    status: str


@dataclass
class _Event:
    client_id: str


def test_no_signals_no_alerts() -> None:
    assert (
        compute_alerts(
            clients=[_Client("c1", "active")],
            threshold_events=[],
            dead_delivery_count=0,
            unhealthy_backends=[],
        )
        == []
    )


def test_all_signals_and_severity_order() -> None:
    alerts = compute_alerts(
        clients=[_Client("c1", "suspended"), _Client("c2", "canceled"), _Client("c3", "active")],
        threshold_events=[_Event("c4"), _Event("c4"), _Event("c5")],  # c4 deduped
        dead_delivery_count=3,
        unhealthy_backends=["vllm-a"],
    )
    kinds = [a.kind for a in alerts]
    # Critical first (backend_unhealthy, client_canceled), then warnings.
    assert alerts[0].severity == "critical"
    assert "backend_unhealthy" in kinds and "client_canceled" in kinds
    assert "client_suspended" in kinds and "webhook_dead_letters" in kinds
    # usage_threshold deduped to one per client (c4, c5).
    usage = [a for a in alerts if a.kind == "usage_threshold"]
    assert {a.target_id for a in usage} == {"c4", "c5"}
    # Dead-letter message pluralizes.
    dead = next(a for a in alerts if a.kind == "webhook_dead_letters")
    assert "3 webhook deliveries are dead-lettered" == dead.message


def test_singular_dead_letter_message() -> None:
    alerts = compute_alerts(
        clients=[], threshold_events=[], dead_delivery_count=1, unhealthy_backends=[]
    )
    assert alerts[0].message == "1 webhook delivery is dead-lettered"
