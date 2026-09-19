"""Health endpoints return their documented responses (Block 0 required)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_readyz_ready_after_migrations(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["migrations"] == "applied"


def test_readyz_does_not_imply_inference_available(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body["inference"]["available"] is False
    assert body["inference"]["reason"]


def test_readyz_reports_not_ready_when_db_missing(tmp_settings) -> None:  # type: ignore[no-untyped-def]
    """If the database becomes unreachable, readiness must report 503."""
    from engine.main import create_app

    app = create_app(tmp_settings)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200
        # Remove the database and its WAL sidecars; the read-only probe must fail.
        for suffix in ("", "-wal", "-shm"):
            p = tmp_settings.db_path.with_name(tmp_settings.db_path.name + suffix)
            if p.exists():
                p.unlink()
        resp = c.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["checks"]["database"].startswith("error")


def test_readyz_uses_per_app_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Two apps in one process each report their own database, not a global."""
    from engine.config import Settings
    from engine.main import create_app

    settings_a = Settings(data_dir=tmp_path / "a")
    settings_b = Settings(data_dir=tmp_path / "b")
    app_a = create_app(settings_a)
    app_b = create_app(settings_b)  # constructed last; a process-global would win

    with TestClient(app_a) as ca, TestClient(app_b) as cb:
        assert ca.get("/readyz").status_code == 200
        assert cb.get("/readyz").status_code == 200
        # Break only app A's database; app B must remain ready.
        for suffix in ("", "-wal", "-shm"):
            p = settings_a.db_path.with_name(settings_a.db_path.name + suffix)
            if p.exists():
                p.unlink()
        assert ca.get("/readyz").status_code == 503
        assert cb.get("/readyz").status_code == 200
