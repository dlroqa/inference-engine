"""Exclusive ownership of an engine's writable store: its database and model directory.

One serving engine (serving any number of clients) owns one writable store.
The engine keeps in-memory ownership of model downloads (see
``engine.models.service``); that is only meaningful if no other engine writes
the same database or the same managed model directory. Startup recovery marks
interrupted downloads, which would be wrong while another engine still owns
them, and one engine's delete could remove another's files. So a serving
engine holds two exclusive, non-blocking locks for its whole lifetime:

- ``<canonical database path>.lock``, next to the database;
- ``<canonical model directory>/.engine-model-store.lock``, inside the managed
  model directory, so engines that reach the same directory through different
  paths or container mounts contend on the same file.

A second engine that shares either resource refuses to start. The database lock
is always taken first and released last; neither wait nor retry.

The operating system releases both locks when the process exits for any reason,
including a forced kill; an unlocked lock file left behind is normal, never
"stale ownership", and is never deleted, truncated or replaced (replacing it
would let two processes lock different files). ``flock`` (POSIX) and
``msvcrt.locking`` (Windows) conflict between two handles in the same process
as well as between processes. They are advisory: they coordinate engines of
this version, not external tools or older releases. Distributed exclusion on
network filesystems (NFS, SMB, FUSE, object-store mounts) is not claimed.
"""

from __future__ import annotations

import errno
import sys
from pathlib import Path
from typing import IO

# The reserved lock file inside a managed model directory. Nothing else in the
# engine creates, lists, imports or deletes it.
MODEL_STORE_LOCK_NAME = ".engine-model-store.lock"


class StoreOwnershipError(RuntimeError):
    """The engine could not take ownership of its store; startup must stop."""


class StoreLockedError(StoreOwnershipError):
    """Another engine process already owns this database or model directory."""


class StoreLockUnavailableError(StoreOwnershipError):
    """A lock could not be taken for another reason (permissions, no lock support)."""


def canonical(path: Path) -> Path:
    """An absolute path with symlinked ancestors and ``..`` resolved.

    Works for paths that do not exist yet (the existing prefix is resolved).
    """
    return Path(path).expanduser().resolve(strict=False)


def lock_path_for(db_path: Path) -> Path:
    """The database lock file, next to the (canonical) database."""
    db = canonical(db_path)
    return db.with_name(db.name + ".lock")


def model_store_lock_path(models_dir: Path) -> Path:
    return canonical(models_dir) / MODEL_STORE_LOCK_NAME


class StoreLock:
    """One exclusive, non-blocking OS lock on one file."""

    def __init__(self, path: Path, *, resource: str = "database") -> None:
        self.path = path
        self.resource = resource
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> None:
        """Takes the lock without waiting.

        Raises :class:`StoreLockedError` if another owner holds it, or
        :class:`StoreLockUnavailableError` if the file cannot be opened or locked
        for another reason. Nothing is held after a failure.
        """
        if self._fh is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # "a+b": create if missing, never truncate or replace an existing file.
            fh = open(self.path, "a+b")  # held open until release()
        except OSError as exc:
            raise StoreLockUnavailableError(
                f"cannot open the {self.resource} lock file {self.path} ({type(exc).__name__})"
            ) from None
        try:
            _lock(fh)
        except OSError as exc:
            fh.close()
            if _is_contention(exc):
                raise StoreLockedError(
                    f"another engine process already owns this {self.resource} "
                    f"(lock: {self.path})"
                ) from None
            raise StoreLockUnavailableError(
                f"cannot lock the {self.resource} lock file {self.path} ({type(exc).__name__})"
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


class StoreOwnership:
    """Both locks of an engine's writable store, taken and released together."""

    def __init__(self, db_path: Path, models_dir: Path) -> None:
        self.database = StoreLock(lock_path_for(db_path), resource="database")
        self.model_store = StoreLock(model_store_lock_path(models_dir), resource="model directory")
        if self.database.path == self.model_store.path:
            raise StoreLockUnavailableError(
                "the database and the model directory map to the same lock file; "
                "configure distinct, non-overlapping locations"
            )

    @property
    def held(self) -> bool:
        return self.database.held or self.model_store.held

    def acquire(self) -> None:
        """Database lock first, then the model directory; all or nothing."""
        self.database.acquire()
        try:
            self.model_store.acquire()
        except BaseException:
            self.database.release()
            raise

    def release(self) -> None:
        """Reverse order: the model directory, then the database."""
        try:
            self.model_store.release()
        finally:
            self.database.release()


if sys.platform == "win32":
    import msvcrt

    def _lock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

    def _is_contention(exc: OSError) -> bool:
        # A region already locked by another handle: EACCES or EDEADLOCK.
        return exc.errno in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK))

else:
    import fcntl

    def _lock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _is_contention(exc: OSError) -> bool:
        return exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)
