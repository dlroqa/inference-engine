"""Model registry: CRUD, single-active enforcement, and progress."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.models.registry import ModelRegistry, ModelStatus
from engine.store.db import connect
from engine.store.migrations import apply_migrations


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    db = tmp_path / "ie.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return ModelRegistry(db)


def _add(registry: ModelRegistry, name: str, status: ModelStatus = ModelStatus.READY):
    return registry.create(
        name=name,
        filename=f"{name}.gguf",
        path=Path(f"/models/{name}.gguf"),
        source_type="url",
        source_ref="http://x/y.gguf",
        status=status,
    )


def test_create_get_list(registry: ModelRegistry) -> None:
    a = _add(registry, "a")
    _add(registry, "b")
    assert registry.get(a.id).name == "a"
    assert {m.name for m in registry.list()} == {"a", "b"}


def test_single_active(registry: ModelRegistry) -> None:
    a = _add(registry, "a")
    b = _add(registry, "b")
    registry.set_active(a.id)
    assert registry.get_active().id == a.id
    registry.set_active(b.id)  # switching moves active atomically
    assert registry.get_active().id == b.id
    assert registry.get(a.id).active is False
    registry.clear_active()
    assert registry.get_active() is None


def test_progress_and_status(registry: ModelRegistry) -> None:
    m = _add(registry, "d", status=ModelStatus.DOWNLOADING)
    registry.update(m.id, size_bytes=1000)
    registry.add_progress(m.id, 250)
    got = registry.get(m.id)
    assert got.size_bytes == 1000
    assert got.downloaded_bytes == 250
    assert got.progress == 0.25
    registry.update(m.id, status=str(ModelStatus.READY))
    assert registry.get(m.id).status == "ready"


def test_delete(registry: ModelRegistry) -> None:
    m = _add(registry, "e")
    assert registry.delete(m.id) is True
    assert registry.get(m.id) is None
    assert registry.delete(m.id) is False
