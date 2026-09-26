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
import threading
import time
from pathlib import Path
from typing import Any

from engine.config import Settings
from engine.inference.llamacpp import LlamaCppBackend
from engine.logging_setup import get_logger
from engine.models import compat, downloader, gguf
from engine.models.redaction import WITHHELD, redact_urls_in_text
from engine.models.registry import ModelRecord, ModelRegistry, ModelStatus

_log = get_logger("engine.models")

# How long shutdown waits for download workers to observe cancellation.
_SHUTDOWN_GRACE_SECONDS = 10.0


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


def _safe_filename(name: str) -> str:
    """Reduce a filename to a safe basename (no path traversal)."""
    base = Path(name).name
    return base or "model.gguf"


class ModelService:
    def __init__(self, settings: Settings, registry: ModelRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.models_dir = Path(settings.models_dir)  # type: ignore[arg-type]
        self._cancels: dict[str, threading.Event] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

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
            fetch_url = url
            source_ref = url
            base = _safe_filename(filename or Path(url.split("?", 1)[0]).name or "model.gguf")
        else:
            raise ModelServiceError(f"unknown source_type: {source_type!r}")

        if not base.endswith(".gguf"):
            base += ".gguf"
        dest = self.models_dir / base
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
        cancel = threading.Event()
        self._cancels[record.id] = cancel
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
        cancel: threading.Event,
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
        # Task cancellation (asyncio.CancelledError) still propagates.
        stage = "download"
        try:
            digest = await asyncio.to_thread(
                downloader.download,
                url,
                dest,
                expected_sha256=expected_sha256,
                chunk_bytes=self.settings.download_chunk_bytes,
                progress_cb=progress,
                cancel=cancel,
                max_bytes=self.settings.max_model_bytes,
            )
            stage = "finalize"
            info = await asyncio.to_thread(gguf.probe, dest)
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
        except asyncio.CancelledError:
            cancel.set()  # the worker thread is not stopped by task cancellation
            self._record_cancelled(model_id)
            raise
        except downloader.DownloadError as exc:
            self._record_failure(model_id, stage, exc, expected=True)
        except Exception as exc:
            self._record_failure(model_id, stage, exc, expected=False)
        else:
            _log.info("model_download_ready", extra={"model_id": model_id})
        finally:
            self._cancels.pop(model_id, None)
            self._tasks.pop(model_id, None)

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

    def _record_failure(self, model_id: str, stage: str, exc: Exception, *, expected: bool) -> None:
        """The single terminal path for a failed download.

        The registry keeps the raw text (API responses redact it on the way
        out); the log line only ever gets the redacted form, computed before
        the logger is called. No exc_info: the exception chain can quote the
        original URL. An unexpected exception is labelled with its stage and
        type, never with the source URL.
        """
        raw = _exception_text(exc)
        if raw is not None and not expected:
            raw = f"download failed during {stage} ({type(exc).__name__}): {raw}"
        self._persist_terminal(model_id, ModelStatus.ERROR, error=WITHHELD if raw is None else raw)
        _log.warning(
            "model_download_failed",
            extra={
                "model_id": model_id,
                "stage": stage,
                "error_type": type(exc).__name__,
                "detail": _log_safe_detail(raw),
            },
        )

    def cancel(self, model_id: str) -> bool:
        cancel = self._cancels.get(model_id)
        if cancel is None:
            return False
        cancel.set()
        return True

    # -- delete ------------------------------------------------------------

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
        """Stop in-flight downloads.

        Signals every worker to cancel, then waits up to ``grace_seconds`` for
        them to stop between chunks and record ``cancelled`` themselves. Tasks
        still running after that are cancelled; their worker threads have the
        cancel event set, write no further progress, and stop at the next chunk.
        """
        for cancel in list(self._cancels.values()):
            cancel.set()
        tasks = list(self._tasks.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
