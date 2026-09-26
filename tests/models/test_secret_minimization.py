"""New durable records never keep any part of a URL download's address.

A path segment or an unknown query parameter can be a credential, so pattern
redaction is not enough: the address is used only by the live download. These
tests put unique synthetic sentinels in every part of the address (userinfo,
path, known, unknown and encoded query names and values, fragment) and in the
exception text a failing transport produces. They check that the transport still
received the original address, and that no sentinel reaches any of these:
database rows, audit events, raw log records or their JSON, API responses,
filenames or model names. No network: ``urlopen`` is replaced.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.logging_setup import JsonFormatter
from engine.main import create_app
from engine.models.service import URL_SOURCE_DESCRIPTOR
from tests.models.test_download_worker_lifetime import WAIT, A, B, GatedResponse

LOOPBACK = ("127.0.0.1", 40000)
MARK = "zq7sentinel"
URL = (
    f"https://svc-{MARK}1:{MARK}2@cdn.example.com/models/{MARK}3/m.gguf"
    f"?X-Custom-Grant{MARK}4={MARK}5&%74oken={MARK}6&opaque={MARK}7#{MARK}8"
)


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_settings), client=LOOPBACK) as c:
        yield c


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    root = logging.getLogger()
    handler = _Capture(level=logging.DEBUG)
    service_logger = logging.getLogger("engine.models")
    old_level = service_logger.level
    service_logger.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield captured
    finally:
        root.removeHandler(handler)
        service_logger.setLevel(old_level)


class _BrokenRead(GatedResponse):
    def read(self, size: int) -> bytes:
        raise OSError(f"connection reset while reading {self.url}")

    url = ""


def _transport(kind: str, seen: list[str]) -> Any:
    """A fake ``urlopen`` that records the address and fails (or succeeds) as ``kind``."""

    def urlopen(request: urllib.request.Request, *args: Any, **kwargs: Any) -> Any:
        url = request.full_url
        seen.append(url)
        if kind == "http_error":
            raise urllib.error.HTTPError(url, 403, f"denied for {url}", Message(), None)
        if kind == "url_error":
            raise urllib.error.URLError(f"cannot reach {url}")
        if kind == "worker_exception":
            raise RuntimeError(f"unexpected failure for {url}")
        if kind == "read_error":
            response = _BrokenRead([A])
            response.url = url
            return response
        return GatedResponse([A, B])  # success

    return urlopen


EXPECTED = {
    "http_error": ("error", "HTTP 403 fetching model"),
    "url_error": ("error", "network error fetching model (URLError)"),
    "read_error": ("error", "network error reading model (OSError)"),
    "worker_exception": ("error", "download failed during download (RuntimeError)"),
    "success": ("ready", None),
}


def _finish(client: TestClient, model_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT
    while True:
        body: dict[str, Any] = client.get(f"/admin/models/{model_id}").json()
        if body["status"] != "downloading":
            return body
        assert time.monotonic() < deadline, "the download did not finish"
        time.sleep(0.02)


def _db_rows(settings: Settings) -> str:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return repr([conn.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables])
    finally:
        conn.close()


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_no_part_of_the_address_is_kept(
    client: TestClient,
    tmp_settings: Settings,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(urllib.request, "urlopen", _transport(kind, seen))
    resp = client.post("/admin/models/download", json={"source_type": "url", "url": URL})
    assert resp.status_code == 200
    assert MARK not in resp.text
    model_id = resp.json()["id"]
    final = _finish(client, model_id)

    status, error = EXPECTED[kind]
    assert final["status"] == status
    assert final["error"] == error
    assert final["source_ref"] == URL_SOURCE_DESCRIPTOR
    assert final["filename"].startswith("download-") and final["name"].startswith("download-")
    # The live download used the original address, sentinels and all.
    assert seen and all(f"{MARK}{n}" in seen[0] for n in range(1, 9))

    assert MARK not in _db_rows(tmp_settings), f"{kind}: kept in the database"
    assert MARK not in client.get("/admin/models").text, f"{kind}: in the API list"
    audit = client.app.state.audit  # type: ignore[attr-defined]
    assert MARK not in repr(audit.list(limit=1000)), f"{kind}: in an audit event"
    for record in records:
        assert MARK not in repr(vars(record)), f"{kind}: in a LogRecord"
        assert MARK not in JsonFormatter().format(record), f"{kind}: in JSON log output"


@pytest.mark.parametrize("filename", ["m.gguf?token=x", "../escape.gguf", "a b.gguf", ".hidden"])
def test_unsafe_operator_filenames_are_rejected(client: TestClient, filename: str) -> None:
    resp = client.post(
        "/admin/models/download", json={"source_type": "url", "url": URL, "filename": filename}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "model_download_failed"
    assert MARK not in resp.text
    assert client.get("/admin/models").json()["models"] == []


def test_operator_filename_and_name_are_used_when_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _transport("success", []))
    body = {"source_type": "url", "url": URL, "filename": "tiny-Q4_K_M.gguf", "name": "tiny"}
    model_id = client.post("/admin/models/download", json=body).json()["id"]
    final = _finish(client, model_id)
    assert final["status"] == "ready"
    assert final["filename"] == "tiny-Q4_K_M.gguf" and final["name"] == "tiny"


def test_malformed_address_is_rejected_without_keeping_it(
    client: TestClient, tmp_settings: Settings
) -> None:
    # No scheme: urllib.request.Request raises ValueError quoting the address.
    url = f"//u:{MARK}9@host.invalid/{MARK}10/m.gguf?k={MARK}11"
    resp = client.post("/admin/models/download", json={"source_type": "url", "url": url})
    model_id = resp.json()["id"]
    final = _finish(client, model_id)
    assert final["status"] == "error" and final["error"] == "invalid model URL (ValueError)"
    assert MARK not in _db_rows(tmp_settings)


def test_hugging_face_references_are_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _transport("url_error", []))
    body = {"source_type": "huggingface", "repo": "org/repo", "filename": "m-Q4.gguf"}
    model_id = client.post("/admin/models/download", json=body).json()["id"]
    final = _finish(client, model_id)
    assert final["source_ref"] == "org/repo/m-Q4.gguf@main"
    assert final["filename"] == "m-Q4.gguf"
