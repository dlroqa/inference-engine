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

from engine.config import Settings
from engine.inference.llamacpp import LlamaCppBackend
from engine.logging_setup import get_logger
from engine.models import compat, downloader, gguf
from engine.models.redaction import WITHHELD, redact_urls_in_text
from engine.models.registry import ModelRecord, ModelRegistry, ModelStatus

_log = get_logger("engine.models")


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
            if total is not None and not seen_total:
                seen_total = True
                self.registry.update(model_id, size_bytes=total)
            now = time.monotonic()
            if now - last >= 0.5:  # throttle DB writes
                last = now
                self.registry.add_progress(model_id, done)

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
        except downloader.DownloadCancelled:
            self.registry.update(model_id, status=str(ModelStatus.CANCELLED))
            _log.info("model_download_cancelled", extra={"model_id": model_id})
        except downloader.DownloadError as exc:
            # The registry keeps the raw text (API responses redact it on the way
            # out); the log line only ever gets the redacted form. No exc_info:
            # the exception chain can quote the original URL.
            raw = _exception_text(exc)
            self.registry.update(
                model_id, status=str(ModelStatus.ERROR), error=WITHHELD if raw is None else raw
            )
            _log.warning(
                "model_download_failed",
                extra={"model_id": model_id, "detail": _log_safe_detail(raw)},
            )
        else:
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
            _log.info("model_download_ready", extra={"model_id": model_id})
        finally:
            self._cancels.pop(model_id, None)
            self._tasks.pop(model_id, None)

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

    async def shutdown(self) -> None:
        for cancel in list(self._cancels.values()):
            cancel.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
