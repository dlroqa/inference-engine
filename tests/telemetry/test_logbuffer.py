"""Log mirror: ring buffer, WARNING+ persistence, and structured columns."""

from __future__ import annotations

import logging
from pathlib import Path

from engine.store.db import connect
from engine.store.migrations import apply_migrations
from engine.telemetry.logbuffer import LogCollector, read_log_events


def _migrated_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ie.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return db_path


def _emit(collector: LogCollector, level: int, msg: str, **extra: object) -> None:
    record = logging.LogRecord(
        name="engine.request",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    collector.emit(record)


def test_ring_captures_all_levels(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    collector = LogCollector(db_path, ring_size=10)
    _emit(collector, logging.INFO, "info_event", request_id="r1")
    _emit(collector, logging.WARNING, "warn_event", request_id="r2")
    recent = collector.recent()
    assert [r["event"] for r in recent] == ["info_event", "warn_event"]
    assert recent[0]["request_id"] == "r1"


def test_ring_is_bounded(tmp_path: Path) -> None:
    collector = LogCollector(None, ring_size=3)
    for i in range(10):
        _emit(collector, logging.INFO, f"e{i}")
    recent = collector.recent()
    assert len(recent) == 3
    assert [r["event"] for r in recent] == ["e7", "e8", "e9"]


def test_warning_and_above_persisted_to_table(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    collector = LogCollector(db_path, db_level=logging.WARNING)
    _emit(collector, logging.INFO, "not_persisted", request_id="r0")
    _emit(
        collector,
        logging.ERROR,
        "request_error",
        request_id="r1",
        route="/v1/chat/completions",
        category="backend",
        stage="generation",
        model="m",
    )
    rows = read_log_events(db_path, limit=50)
    events = {row["event"] for row in rows}
    assert "request_error" in events
    assert "not_persisted" not in events  # INFO stays out of the table
    row = next(r for r in rows if r["event"] == "request_error")
    assert row["request_id"] == "r1"
    assert row["category"] == "backend"
    assert row["stage"] == "generation"


def test_read_filters_by_request_and_level(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    collector = LogCollector(db_path, db_level=logging.WARNING)
    _emit(collector, logging.WARNING, "a", request_id="r1")
    _emit(collector, logging.ERROR, "b", request_id="r2")
    assert [r["event"] for r in read_log_events(db_path, request_id="r2")] == ["b"]
    assert [r["event"] for r in read_log_events(db_path, level="WARNING")] == ["a"]
