"""Downloader: checksum verify, resume, cancel, and HF URL building."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
