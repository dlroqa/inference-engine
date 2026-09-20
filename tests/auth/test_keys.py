"""API-key store: issuance, verification, revocation, hashing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from engine.auth.keys import KEY_PREFIX, KeyStore, hash_token
from engine.store.db import connect
from engine.store.migrations import apply_migrations


def _store(tmp_path: Path) -> KeyStore:
    db = tmp_path / "keys.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return KeyStore(db)


def test_create_returns_token_once_and_hashes_at_rest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record, token = store.create(label="ci")
    assert token.startswith(KEY_PREFIX)
    assert record.label == "ci"
    assert record.prefix == token[:12]

    # The plaintext token is never stored; only its hash.
    conn = sqlite3.connect(str(tmp_path / "keys.db"))
    rows = conn.execute("SELECT key_hash FROM api_keys;").fetchall()
    conn.close()
    assert rows[0][0] == hash_token(token)
    assert token not in {r[0] for r in rows}


def test_verify_valid_and_updates_last_used(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record, token = store.create()
    assert store.get(record.id).last_used_at is None  # type: ignore[union-attr]
    verified = store.verify(token)
    assert verified is not None and verified.id == record.id
    assert store.get(record.id).last_used_at is not None  # type: ignore[union-attr]


def test_verify_rejects_unknown_and_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.verify("sk-ie-nope") is None
    assert store.verify("") is None


def test_revoke_rejects_afterwards(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record, token = store.create()
    assert store.revoke(record.id) is True
    assert store.verify(token) is None
    assert store.revoke(record.id) is False  # already revoked
    assert store.get(record.id).revoked is True  # type: ignore[union-attr]


def test_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(label="a")
    store.create(label="b")
    labels = {r.label for r in store.list()}
    assert labels == {"a", "b"}
