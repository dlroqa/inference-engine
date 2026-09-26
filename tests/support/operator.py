"""Test helper: a TestClient that authenticates as an operator.

With effective auth on, operator surfaces require an operator key even from
loopback (credentials are checked before the loopback dev exception). Tests
that exercise admin endpoints under ``require_auth=True`` use this client; a
per-request ``Authorization`` header still overrides it (e.g. a client key for
inference).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.auth.keys import KeyStore
from engine.config import Settings

LOOPBACK = ("127.0.0.1", 40000)


@contextmanager
def operator_client(
    app: FastAPI, settings: Settings, peer: tuple[str, int] = LOOPBACK
) -> Iterator[TestClient]:
    with TestClient(app, client=peer) as client:  # lifespan applies migrations
        store = KeyStore(settings.db_path)  # type: ignore[arg-type]
        _record, token = store.create(label="test-operator")
        client.headers["authorization"] = f"Bearer {token}"
        yield client
