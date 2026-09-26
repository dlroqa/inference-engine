"""Downloader: checksum verify, resume, cancel, and HF URL building."""

from __future__ import annotations

import hashlib
import http.client
import socket
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from engine.models import downloader

PAYLOAD = b"0123456789abcdef" * 4096  # 64 KiB
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence
        pass

    def do_GET(self) -> None:
        ignore_range = self.path.startswith("/noresume")
        rng = self.headers.get("Range")
        if rng and not ignore_range and rng.startswith("bytes="):
            start = int(rng[len("bytes=") :].split("-", 1)[0])
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_download_and_verify(server: str, tmp_path: Path) -> None:
    dest = tmp_path / "m.gguf"
    digest = downloader.download(f"{server}/model", dest, expected_sha256=DIGEST, chunk_bytes=4096)
    assert digest == DIGEST
    assert dest.read_bytes() == PAYLOAD
    assert not dest.with_suffix(".gguf.part").exists()


def test_checksum_mismatch_raises(server: str, tmp_path: Path) -> None:
    dest = tmp_path / "m.gguf"
    with pytest.raises(downloader.ChecksumMismatch):
        downloader.download(f"{server}/model", dest, expected_sha256="deadbeef")


def test_resume_from_partial(server: str, tmp_path: Path) -> None:
    dest = tmp_path / "m.gguf"
    part = dest.with_suffix(".gguf.part")
    part.write_bytes(PAYLOAD[: len(PAYLOAD) // 2])  # pre-existing half
    digest = downloader.download(f"{server}/model", dest, expected_sha256=DIGEST, chunk_bytes=4096)
    assert digest == DIGEST
    assert dest.read_bytes() == PAYLOAD


def test_server_ignoring_range_restarts(server: str, tmp_path: Path) -> None:
    dest = tmp_path / "m.gguf"
    dest.with_suffix(".gguf.part").write_bytes(b"garbage-partial")
    # /noresume returns 200 (full) even with a Range header -> download restarts.
    digest = downloader.download(f"{server}/noresume", dest, expected_sha256=DIGEST)
    assert digest == DIGEST
    assert dest.read_bytes() == PAYLOAD


def test_cancel_before_start(server: str, tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(downloader.DownloadCancelled):
        downloader.download(f"{server}/model", tmp_path / "m.gguf", cancel=cancel)


def test_http_error_raises(server: str, tmp_path: Path) -> None:
    # A bad host -> network error.
    with pytest.raises(downloader.DownloadError):
        downloader.download("http://127.0.0.1:9/nope", tmp_path / "m.gguf")


def test_hf_url() -> None:
    url = downloader.hf_url("https://huggingface.co", "org/repo", "model-Q4.gguf")
    assert url == "https://huggingface.co/org/repo/resolve/main/model-Q4.gguf"


# Invalid URLs: the real exception types from request construction and request
# opening become DownloadError before any connection is attempted, and the
# message never quotes the URL. Credentials here are synthetic.


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records (and refuses) every outbound connection attempt."""
    attempts: list[object] = []

    def refuse(*args: object, **kwargs: object) -> socket.socket:
        attempts.append(args)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        # urllib.request.Request raises ValueError ("unknown url type: '<url>'").
        ("//u:synthetic-pw-201@host.invalid/m.gguf?token=synthetic-tok-201", "ValueError"),
        # http.client raises InvalidURL while opening ("URL can't contain control characters").
        ("http://127.0.0.1:9/m.gguf?token=synthetic-tok-202 x", "InvalidURL"),
    ],
)
def test_invalid_urls_fail_before_network_without_quoting_the_url(
    tmp_path: Path, connections: list[object], url: str, kind: str
) -> None:
    with pytest.raises(downloader.DownloadError) as raised:
        downloader.download(url, tmp_path / "m.gguf")
    assert str(raised.value) == f"invalid model URL ({kind})"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert connections == [], "a connection was attempted"


def test_invalid_url_raised_while_opening_is_converted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rejects(*args: object, **kwargs: object) -> object:
        raise http.client.InvalidURL("nonnumeric port: 'synthetic-pw-203'")

    monkeypatch.setattr(urllib.request, "urlopen", rejects)
    with pytest.raises(downloader.DownloadError) as raised:
        downloader.download("http://h.example/m.gguf", tmp_path / "m.gguf")
    assert "synthetic" not in str(raised.value)


class _BrokenResponse:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.closed = False

    def read(self, size: int) -> bytes:
        raise http.client.IncompleteRead(b"partial")

    def close(self) -> None:
        self.closed = True


def test_read_failure_is_a_download_error_and_closes_the_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _BrokenResponse()

    def opens(*args: Any, **kwargs: Any) -> _BrokenResponse:
        return response

    monkeypatch.setattr(urllib.request, "urlopen", opens)
    with pytest.raises(downloader.DownloadError, match="network error reading model"):
        downloader.download("http://h.example/m.gguf", tmp_path / "m.gguf")
    assert response.closed
    assert not (tmp_path / "m.gguf").exists()
