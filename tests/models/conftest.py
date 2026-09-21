"""Shared fixtures for model-lifecycle tests: a local HTTP server serving a real
(tiny) GGUF payload with Range support, so downloads probe + verify end to end."""

from __future__ import annotations

import hashlib
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.support.gguf_writer import write_gguf


def _build_payload() -> bytes:
    with tempfile.TemporaryDirectory() as d:
        path = write_gguf(Path(d) / "m.gguf", name="Served", context_length=2048, file_type=15)
        # Pad so the file is comfortably larger than one chunk.
        data = path.read_bytes()
    return data + b"\0" * 40_000


PAYLOAD = _build_payload()
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def _make_handler(payload: bytes) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng[len("bytes=") :].split("-", 1)[0])
                body = payload[start:]
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                )
            else:
                body = payload
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


@dataclass
class FileServer:
    base_url: str
    digest: str


@pytest.fixture
def gguf_server() -> Iterator[FileServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(PAYLOAD))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield FileServer(base_url=f"http://127.0.0.1:{httpd.server_address[1]}", digest=DIGEST)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
