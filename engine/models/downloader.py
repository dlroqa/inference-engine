"""Checksum-verified, resumable model downloads.

Streams a GGUF file to ``<dest>.part`` and atomically renames it into place only
after an optional SHA-256 check passes. Downloads resume from a partial file via
an HTTP ``Range`` request, so an interrupted transfer is recoverable rather than
restarted. A ``threading.Event`` cancels between chunks (leaving the ``.part`` for
a later resume). Blocking by design — callers run it in a worker thread.

Only ``http(s)`` public URLs are supported; gated/authenticated Hugging Face repos
are out of scope for this block.
"""

from __future__ import annotations

import hashlib
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ProgressCallback = Callable[[int, int | None], None]


class DownloadError(Exception):
    """A download failed (network, HTTP, or size-limit)."""


class DownloadCancelled(Exception):
    """The download was cancelled; the partial file is kept for resume."""


class ChecksumMismatch(DownloadError):
    """The downloaded file's SHA-256 did not match the expected value."""


def hf_url(endpoint: str, repo: str, filename: str, revision: str = "main") -> str:
    """Build a Hugging Face ``resolve`` URL for a repo file."""
    endpoint = endpoint.rstrip("/")
    return f"{endpoint}/{repo}/resolve/{revision}/{filename}"


def sha256_file(path: Path, chunk_bytes: int = 1_048_576) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_bytes)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def download(
    url: str,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    chunk_bytes: int = 1_048_576,
    progress_cb: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    max_bytes: int = 0,
) -> str:
    """Download ``url`` to ``dest`` (resumable). Returns the file's SHA-256.

    Raises :class:`DownloadCancelled` if cancelled, :class:`ChecksumMismatch` on a
    checksum failure, or :class:`DownloadError` on network/HTTP/size errors.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    resume_from = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": "inference-engine"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"

    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=30)  # noqa: S310 (http(s) only)
    except urllib.error.HTTPError as exc:
        if resume_from and exc.code == 416:  # range not satisfiable -> restart clean
            part.unlink(missing_ok=True)
            return download(
                url,
                dest,
                expected_sha256=expected_sha256,
                chunk_bytes=chunk_bytes,
                progress_cb=progress_cb,
                cancel=cancel,
                max_bytes=max_bytes,
            )
        raise DownloadError(f"HTTP {exc.code} fetching model") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(f"network error fetching model: {exc}") from exc

    # If the server ignored the Range header (200 not 206), start over.
    partial = response.status == 206
    if resume_from and not partial:
        resume_from = 0
        part.unlink(missing_ok=True)

    total: int | None = None
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        total = int(length) + (resume_from if partial else 0)
    if max_bytes and total and total > max_bytes:
        response.close()
        raise DownloadError(f"model exceeds max_model_bytes ({total} > {max_bytes})")

    downloaded = resume_from
    mode = "ab" if (resume_from and partial) else "wb"
    try:
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("download cancelled")
                chunk = response.read(chunk_bytes)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if max_bytes and downloaded > max_bytes:
                    raise DownloadError(f"model exceeds max_model_bytes ({max_bytes})")
                if progress_cb is not None:
                    progress_cb(downloaded, total)
    finally:
        response.close()

    digest = sha256_file(part, chunk_bytes)
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        raise ChecksumMismatch(f"checksum mismatch: expected {expected_sha256}, got {digest}")
    part.replace(dest)
    return digest
