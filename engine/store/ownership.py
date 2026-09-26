"""Exclusive ownership of a data store by one serving engine process.

The engine keeps in-memory ownership of model downloads (see
``engine.models.service``). That is only meaningful if no other engine process
serves the same database: startup recovery marks interrupted downloads, which
would be wrong while another process still owns them. So a serving engine
holds an exclusive, non-blocking lock on ``<database>.lock`` for its whole
lifetime, and a second engine on the same store refuses to start.

The operating system releases the lock when the process exits for any reason,
including a forced kill, so a crash never leaves a stale lock behind.
``flock`` (POSIX) and ``msvcrt.locking`` (Windows) both conflict between two
handles in the same process as well as between processes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO


class StoreLockedError(RuntimeError):
    """Another engine process is already serving this data store."""


def lock_path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".lock")


class StoreLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        """Takes the lock without waiting; raises :class:`StoreLockedError` if held."""
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # held open until release()
        try:
            _lock(fh)
        except OSError:
            fh.close()
            raise StoreLockedError(
                f"another engine process is already serving this data store (lock: {self.path})"
            ) from None
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()


if sys.platform == "win32":
    import msvcrt

    def _lock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
