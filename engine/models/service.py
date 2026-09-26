"""Model lifecycle orchestration.

Ties the registry, GGUF probe, downloader, and llama.cpp backend together and owns
the background download tasks. The API layer calls this service and swaps
``app.state.backend`` on load/unload; the service never touches app state itself.

Downloads run in a worker thread (blocking urllib) driven from an asyncio task, so
the event loop stays responsive; progress is persisted to the registry (throttled)
for the dashboard to poll. Cancellation and app shutdown stop in-flight downloads,
leaving a resumable ``.part`` file.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from engine.config import Settings
from engine.inference.llamacpp import LlamaCppBackend
from engine.logging_setup import get_logger
from engine.models import compat, downloader, gguf
from engine.models.redaction import WITHHELD, redact_urls_in_text
from engine.models.registry import ModelRecord, ModelRegistry, ModelStatus

_log = get_logger("engine.models")

# How long shutdown waits for download workers to stop before it warns. This is
# a cooperative interval, not a bound on shutdown: workers are never abandoned.
_SHUTDOWN_GRACE_SECONDS = 10.0

_T = TypeVar("_T")


def _exception_text(exc: BaseException) -> str | None:
    """``str(exc)``, or None if the exception cannot be converted."""
    try:
        return str(exc)
    except Exception:
        return None


def _log_safe_detail(text: str | None) -> str:
    """A download failure's text with credentials removed, for a log line.

    Computed before the logger is called, so no handler, filter or formatter
    ever sees the raw text. If redaction fails, a fixed message is used
    instead; the raw text is never the fallback.
    """
    if text is None:
        return WITHHELD
    try:
        return redact_urls_in_text(text) or "download failed"
    except Exception:
        return WITHHELD


class ModelServiceError(Exception):
    """A model-management operation failed (bad input, missing file, conflict)."""


class ModelBusyError(ModelServiceError):
    """Work the service owns is still running for this model or its file."""


class ModelDeleteError(ModelServiceError):
    """Deleting a model failed part-way; the registry row is kept for a retry."""


def _path_key(path: Path) -> str:
    """A stable key for a model file path (resolved when possible)."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path).absolute())


# What a URL download records as its source. The address itself is used only
# for the live download: a path segment or an unknown query parameter can be a
# credential, so no part of it is stored, logged, or used to name the model.
URL_SOURCE_DESCRIPTOR = "address not stored"

# Recorded for a download that was still running when the engine last stopped.
INTERRUPTED = (
    "download interrupted: the engine stopped before it finished. Delete this model "
    "(which removes any file it kept) and download it again."
)

# An operator-supplied filename for a URL download.
_OPERATOR_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _url_download_filename(filename: str | None) -> str:
    """A generated name, or the operator's filename if it is plainly safe."""
    if not filename:
        return f"download-{uuid.uuid4().hex[:12]}.gguf"
    if not _OPERATOR_FILENAME.fullmatch(filename):
        raise ModelServiceError(
            "filename may contain only letters, digits, '.', '_' and '-' (at most 128)"
        )
    return filename


def _observed_files(path: Path) -> str:
    """Which of a download's files exist (fixed labels; links are not followed)."""
    promoted = os.path.lexists(path)
    partial = os.path.lexists(path.with_suffix(path.suffix + ".part"))
    if promoted and partial:
        return "both"
    return "promoted" if promoted else "partial" if partial else "neither"


def _safe_filename(name: str) -> str:
    """Reduce a filename to a safe basename (no path traversal)."""
    base = Path(name).name
    return base or "model.gguf"


class ModelService:
    def __init__(self, settings: Settings, registry: ModelRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.models_dir = Path(settings.models_dir)  # type: ignore[arg-type]
        self._cancels: dict[str, downloader.CancelEvent] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Ownership the delete decision checks. A download is owned from its
        # start until its worker has stopped (``_tasks``); its destination is
        # reserved for that long. Loads hold their model id, imports their path.
        self._download_paths: dict[str, str] = {}
        self._operations: set[str] = set()
        self._import_paths: set[str] = set()

    # -- metadata / compat -------------------------------------------------

    def compat_for(self, record: ModelRecord) -> dict[str, str]:
        return compat.assess(
            size_bytes=record.size_bytes,
            valid_gguf=record.status == ModelStatus.READY.value or record.arch is not None,
        ).to_dict()

    def build_backend(self, record: ModelRecord) -> LlamaCppBackend:
        """Construct (not load) a backend for a registry model."""
        return LlamaCppBackend(
            model_path=Path(record.path),
            model_id=record.name,
            n_ctx=record.context_length or self.settings.n_ctx,
            n_threads=self.settings.n_threads,
            n_gpu_layers=self.settings.n_gpu_layers,
        )

    # -- import (local file, in place) -------------------------------------

    async def import_local(self, path: Path, name: str | None = None) -> ModelRecord:
        path = Path(path).expanduser()
        key = _path_key(path)
        # Reserve the path before the first await, so a delete cannot remove
        # the file being imported and no in-flight download's file is claimed.
        if key in self._import_paths or key in self._download_paths.values():
            raise ModelBusyError("another operation is using this model file")
        self._import_paths.add(key)
        try:
            if not path.is_file():
                raise ModelServiceError(f"file not found: {path}")
            info = gguf.probe(path)
            if not info.valid:
                raise ModelServiceError(f"not a valid GGUF file: {path}")
            # Hash off the event loop; imported files can be large.
            digest = await asyncio.to_thread(downloader.sha256_file, path)
            size = path.stat().st_size
            return self.registry.create(
                name=name or info.name or path.stem,
                filename=path.name,
                path=path,
                source_type="import",
                source_ref=str(path),
                status=ModelStatus.READY,
                sha256=digest,
                size_bytes=size,
                quant=info.quant,
                arch=info.arch,
                context_length=info.context_length,
            )
        finally:
            self._import_paths.discard(key)

    def register_configured(self, path: Path, name: str) -> ModelRecord | None:
        """Register an already-configured model file on startup (no hashing).

        Best-effort: probes metadata and records the model as ready+active so the
        dashboard shows the configured model. Returns None if the file is missing.
        """
        path = Path(path)
        if not path.is_file():
            return None
        existing = self.registry.find_by_path(path)
        if existing is not None:
            return existing
        info = gguf.probe(path)
        record = self.registry.create(
            name=name,
            filename=path.name,
            path=path,
            source_type="import",
            source_ref=str(path),
            status=ModelStatus.READY,
            size_bytes=path.stat().st_size,
            quant=info.quant,
            arch=info.arch,
            context_length=info.context_length,
        )
        self.registry.set_active(record.id)
        return record

    # -- download ----------------------------------------------------------

    def start_download(
        self,
        *,
        source_type: str,
        name: str | None = None,
        repo: str | None = None,
        filename: str | None = None,
        revision: str = "main",
        url: str | None = None,
        expected_sha256: str | None = None,
    ) -> ModelRecord:
        if source_type == "huggingface":
            if not repo or not filename:
                raise ModelServiceError("huggingface downloads require 'repo' and 'filename'")
            fetch_url = downloader.hf_url(self.settings.hf_endpoint, repo, filename, revision)
            source_ref = f"{repo}/{filename}@{revision}"
            base = _safe_filename(filename)
        elif source_type == "url":
            if not url:
                raise ModelServiceError("url downloads require 'url'")
            fetch_url = url  # used only by the live download, never stored
            source_ref = URL_SOURCE_DESCRIPTOR
            base = _url_download_filename(filename)
        else:
            raise ModelServiceError(f"unknown source_type: {source_type!r}")

        if not base.endswith(".gguf"):
            base += ".gguf"
        dest = self.models_dir / base
        if _path_key(dest) in self._import_paths:
            raise ModelBusyError("another operation is using this model file")
        if dest.exists() or self.registry.find_by_path(dest) is not None:
            raise ModelServiceError(f"a model file named {base!r} already exists")

        record = self.registry.create(
            name=name or Path(base).stem,
            filename=base,
            path=dest,
            source_type=source_type,
            source_ref=source_ref,
            status=ModelStatus.DOWNLOADING,
            expected_sha256=expected_sha256,
        )
        cancel = downloader.CancelEvent()
        self._cancels[record.id] = cancel
        self._download_paths[record.id] = _path_key(dest)
        self._tasks[record.id] = asyncio.create_task(
            self._run_download(record.id, fetch_url, dest, expected_sha256, cancel)
        )
        return record

    async def _run_download(
        self,
        model_id: str,
        url: str,
        dest: Path,
        expected_sha256: str | None,
        cancel: downloader.CancelEvent,
    ) -> None:
        last = 0.0
        seen_total = False

        def progress(done: int, total: int | None) -> None:
            nonlocal last, seen_total
            if cancel.is_set():  # cancelled or shutting down: no late writes
                return
            if total is not None and not seen_total:
                seen_total = True
                self.registry.update(model_id, size_bytes=total)
            now = time.monotonic()
            if now - last >= 0.5:  # throttle DB writes
                last = now
                self.registry.add_progress(model_id, done)

        # This task is fire-and-forget: every ordinary exception ends here, in a
        # recorded terminal state with a sanitized log line, and never escapes
        # to asyncio's unhandled-task reporting (which would print it raw).
        #
        # It also owns its worker threads. Cancelling the task (directly, or by
        # loop teardown) cannot stop a thread, so it sets the cooperative
        # cancel event and keeps waiting for the thread; only then is the
        # outcome recorded, tracking released, and CancelledError re-raised.
        interrupted = False

        async def owned(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
            nonlocal interrupted
            loop = asyncio.get_running_loop()
            call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
            # A plain executor future, not a task, so nothing else cancels it.
            work: asyncio.Future[_T] = loop.run_in_executor(None, call)
            while True:
                try:
                    return await asyncio.shield(work)
                except asyncio.CancelledError:
                    interrupted = True
                    cancel.set()

        stage = "download"
        try:
            digest = await owned(
                downloader.download,
                url,
                dest,
                expected_sha256=expected_sha256,
                chunk_bytes=self.settings.download_chunk_bytes,
                progress_cb=progress,
                cancel=cancel,
                max_bytes=self.settings.max_model_bytes,
            )
            # The file is now promoted, so the download has committed: later
            # cancel requests are refused and it finishes as ready (or error).
            stage = "finalize"
            info = await owned(gguf.probe, dest)
            size = dest.stat().st_size
            self.registry.update(
                model_id,
                status=str(ModelStatus.READY),
                sha256=digest,
                size_bytes=size,
                downloaded_bytes=size,
                quant=info.quant,
                arch=info.arch,
                context_length=info.context_length,
                error=None,
            )
        except downloader.DownloadCancelled:
            self._record_cancelled(model_id)
        except Exception as exc:
            self._record_failure(model_id, stage, exc)
        else:
            _log.info("model_download_ready", extra={"model_id": model_id})
        finally:
            self._release(model_id)
        if interrupted:
            raise asyncio.CancelledError

    def _release(self, model_id: str) -> None:
        """Ends the service's ownership of a download (its worker has stopped)."""
        self._cancels.pop(model_id, None)
        self._tasks.pop(model_id, None)
        self._download_paths.pop(model_id, None)

    def _persist_terminal(self, model_id: str, status: ModelStatus, **fields: Any) -> bool:
        """One attempt to record a terminal state; a failure is reported, not raised.

        The raw database error is not logged (only its type), and the update is
        not retried: if the store is unavailable, the log says the state was not
        recorded instead of claiming a transition that did not persist.
        """
        try:
            self.registry.update(model_id, status=str(status), **fields)
            return True
        except Exception as exc:
            _log.warning(
                "model_download_state_not_recorded",
                extra={
                    "model_id": model_id,
                    "status": str(status),
                    "error_type": type(exc).__name__,
                },
            )
            return False

    def _record_cancelled(self, model_id: str) -> None:
        self._persist_terminal(model_id, ModelStatus.CANCELLED)
        _log.info("model_download_cancelled", extra={"model_id": model_id})

    def _record_failure(self, model_id: str, stage: str, exc: Exception) -> None:
        """The single terminal path for a failed download.

        Only a message the downloader built from fixed text, numbers and type
        names (``DownloadError.safe``) is stored and logged. Any other exception
        is recorded as its stage and type, never its text: exception text can
        quote the URL or a server's reply, and not every secret has a known
        shape. The log line also passes through the shared redaction, computed
        before the logger is called; no exc_info (the chain can quote the URL).
        """
        label = f"download failed during {stage} ({type(exc).__name__})"
        message: str | None = label
        if isinstance(exc, downloader.DownloadError) and exc.safe:
            message = _exception_text(exc)
        self._persist_terminal(
            model_id, ModelStatus.ERROR, error=WITHHELD if message is None else message
        )
        _log.warning(
            "model_download_failed",
            extra={
                "model_id": model_id,
                "stage": stage,
                "error_type": type(exc).__name__,
                "detail": _log_safe_detail(message),
            },
        )

    def cancel(self, model_id: str) -> bool:
        """Requests cancellation; False if not downloading or already committed."""
        cancel = self._cancels.get(model_id)
        if cancel is None:
            return False
        return cancel.request()

    # -- restart recovery --------------------------------------------------

    def reconcile_interrupted(self) -> int:
        """Marks downloads left running by a previous engine process as failed.

        Called at startup, while this process holds the store lock (so no other
        engine can own them) and before any model operation is admitted. It is
        conservative: whatever files remain, the row becomes ``error`` with a
        fixed message; files are kept and never inspected beyond existence
        (symlinks are not followed), and nothing is marked ready or loaded.
        Updates are conditional, so a repeated startup changes nothing. A
        database error propagates: startup fails rather than claim recovery.
        """
        running = (str(ModelStatus.DOWNLOADING), str(ModelStatus.VERIFYING))
        changed = 0
        for record in self.registry.list():
            if record.status not in running or record.id in self._tasks:
                continue
            files = _observed_files(Path(record.path))
            if self.registry.mark_interrupted(record.id, from_statuses=running, error=INTERRUPTED):
                changed += 1
                _log.warning(
                    "model_download_interrupted",
                    extra={"model_id": record.id, "files": files},
                )
        return changed

    # -- ownership and delete ---------------------------------------------

    def is_busy(self, record: ModelRecord) -> bool:
        """Whether work the service owns is running for this model or its file.

        A download counts from its start until its worker has stopped, including
        after a cancel request and during post-download finalization; a load or
        an import of the same file counts while it runs.
        """
        return (
            record.id in self._tasks
            or record.id in self._operations
            or _path_key(Path(record.path)) in self._import_paths
        )

    @contextmanager
    def operation(self, record: ModelRecord) -> Iterator[None]:
        """Holds a model for an operation that awaits (a load): delete refuses meanwhile."""
        if self.is_busy(record):
            raise ModelBusyError("another operation on this model is in progress")
        self._operations.add(record.id)
        try:
            yield
        finally:
            self._operations.discard(record.id)

    def delete_model(self, record: ModelRecord) -> None:
        """Deletes a model's managed files, then its registry row.

        Refuses (without cancelling anything or touching files) while the
        service owns work for the model. Runs without awaiting, so nothing can
        start using the model between the decision and the deletion. If
        removing a file or the row fails, the row is kept and a repeated delete
        converges (missing files are skipped).
        """
        if self.is_busy(record):
            raise ModelBusyError("the model has a download, load or import in progress")
        try:
            self.delete_files(record)
            self.registry.delete(record.id)
        except Exception as exc:
            raise ModelDeleteError(type(exc).__name__) from None

    def delete_files(self, record: ModelRecord) -> None:
        """Remove a model's on-disk files (only files we manage under models_dir)."""
        path = Path(record.path)
        # Imported-in-place files that live outside models_dir are left on disk;
        # only remove files the engine downloaded into its own store.
        if record.source_type != "import" or self._within_models_dir(path):
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)

    def _within_models_dir(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.models_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    async def shutdown(self, grace_seconds: float = _SHUTDOWN_GRACE_SECONDS) -> None:
        """Stop in-flight downloads and wait until their workers have stopped.

        Signals every download to cancel and waits for each to finish: its
        worker has returned, its file handles are closed, and its outcome is
        recorded. If that takes longer than ``grace_seconds``, a fixed warning
        is logged and shutdown keeps waiting; it never returns while a worker
        can still change model files. How long that takes depends on the
        worker (a blocked network read times out after 30 seconds). A
        download that has already committed its file finishes as ready.
        Safe to call repeatedly.
        """
        warned = False
        while self._tasks:
            for cancel in list(self._cancels.values()):
                cancel.set()
            tasks = list(self._tasks.values())
            # asyncio.wait (unlike gather) never cancels the tasks it waits on.
            _, pending = await asyncio.wait(tasks, timeout=None if warned else grace_seconds)
            if pending and not warned:
                warned = True
                _log.warning(
                    "model_download_shutdown_waiting",
                    extra={"downloads": len(pending), "grace_seconds": grace_seconds},
                )
            # A task cancelled before it ever ran never reached its own cleanup
            # (and never started a worker): release it here.
            for model_id, task in list(self._tasks.items()):
                if task.done():
                    self._release(model_id)
