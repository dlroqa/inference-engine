"""A disposable engine-like process for restart-recovery tests.

Run as ``python -m tests.support.download_child <data_dir> <barrier> <marker>``.
It takes the store lock (as a serving engine does), starts a real URL download
through ``ModelService`` against a controlled fake response, and stops at a
deterministic barrier:

- ``read``: blocked in a network read, before the file is promoted;
- ``finalize``: after the rename, blocked in the post-download probe, before
  the registry is updated.

On reaching the barrier it writes ``marker`` and waits forever. The test then
kills it (SIGKILL / TerminateProcess) and restarts on the same data store.
No network is used.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

from engine.config import Settings
from engine.models import gguf
from engine.models.registry import ModelRegistry
from engine.models.service import ModelService
from engine.store.db import connect
from engine.store.migrations import apply_migrations
from engine.store.ownership import StoreLock, lock_path_for

A = b"A" * 8
B = b"B" * 8


class _Response:
    status = 200

    def __init__(self, chunks: list[bytes], block_at: int | None, on_block: Any) -> None:
        self._chunks = chunks
        self._block_at = block_at
        self._on_block = on_block
        self._reads = 0
        self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}

    def read(self, size: int) -> bytes:
        index = self._reads
        self._reads += 1
        if index == self._block_at:
            self._on_block()
        return self._chunks[index] if index < len(self._chunks) else b""

    def close(self) -> None:
        pass


def main(data_dir: str, barrier: str, marker: str) -> None:
    settings = Settings(data_dir=Path(data_dir))
    lock = StoreLock(lock_path_for(settings.db_path))  # type: ignore[arg-type]
    lock.acquire()  # held until this process is killed
    conn = connect(settings.db_path)  # type: ignore[arg-type]
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    forever = threading.Event()

    def reached() -> None:
        Path(marker).write_text(barrier)
        forever.wait()

    block_at = 1 if barrier == "read" else None
    response = _Response([A, B], block_at, reached)

    def urlopen(*args: Any, **kwargs: Any) -> _Response:
        return response

    urllib.request.urlopen = urlopen  # type: ignore[assignment]
    if barrier == "finalize":

        def probe(path: Path) -> gguf.GgufInfo:
            reached()
            raise AssertionError("unreachable")

        gguf.probe = probe  # type: ignore[assignment]

    service = ModelService(settings, ModelRegistry(settings.db_path))  # type: ignore[arg-type]

    async def run() -> None:
        service.start_download(
            source_type="url", url="https://cdn.example.com/m.gguf", filename="m.gguf"
        )
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main(*sys.argv[1:4])
