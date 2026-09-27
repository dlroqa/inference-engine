"""Checksum-verified, resumable model downloads.

Streams a GGUF file to ``<dest>.part`` and atomically renames it into place only
after an optional SHA-256 check passes. Downloads resume from a partial file via
an HTTP ``Range`` request, so an interrupted transfer is recoverable rather than
restarted. A ``threading.Event`` cancels between chunks (leaving the ``.part`` for
a later resume). Blocking by design — callers run it in a worker thread.

Cancellation is checked before any network or file work, after every read (so
bytes or an EOF that a blocked read returns after a cancel are not accepted),
between checksum chunks, and at the commit point: promoting ``.part`` to the
destination. With a :class:`CancelEvent`, the commit point is atomic with
respect to a cancel request: a cancel accepted before promotion prevents it,
and once the file is promoted, later cancel requests are refused.

Only ``http(s)`` public URLs are supported; gated/authenticated Hugging Face repos
are out of scope for this block.
"""

from __future__ import annotations

import hashlib
import http.client
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ProgressCallback = Callable[[int, int | None], None]


class DownloadError(Exception):
    """A download failed (network, HTTP, or size-limit).

    ``safe`` marks a message built only from fixed text, numbers and exception
    type names: never a URL, a path, or text a server or the OS supplied. Every
    error this module raises is safe. The model service stores and logs only
    safe messages; anything else is recorded as a fixed label.
    """

    def __init__(self, message: str, *, safe: bool = False) -> None:
        super().__init__(message)
        self.safe = safe


class DownloadCancelled(Exception):
    """The download was cancelled; the partial file is kept for resume."""


class ChecksumMismatch(DownloadError):
    """The downloaded file's SHA-256 did not match the expected value."""


class CancelEvent(threading.Event):
    """A cooperative cancel flag with a commit point (the final rename).

    ``set()`` / :meth:`request` and :meth:`commit` share a lock held only for
    the local rename, never across network reads, so a cancel request is
    never delayed by a blocked download.
    """

    def __init__(self) -> None:
        super().__init__()
        self._commit_lock = threading.Lock()
        self._committed = False

    @property
    def committed(self) -> bool:
        return self._committed

    def set(self) -> None:
        self.request()

    def request(self) -> bool:
        """Requests cancellation; False if the download has already committed."""
        with self._commit_lock:
            if self._committed:
                return False
            super().set()
            return True

    def commit(self, promote: Callable[[], object]) -> bool:
        """Runs ``promote`` unless cancellation was accepted first."""
        with self._commit_lock:
            if self.is_set():
                return False
            promote()
            self._committed = True
            return True


def _describe(exc: BaseException) -> str:
    """A transport failure as type names and an errno only (no message text)."""
    reason = getattr(exc, "reason", None)
    cause = reason if isinstance(reason, BaseException) else exc
    errno = getattr(cause, "errno", None)
    text = type(cause).__name__
    return f"{text}, errno {errno}" if isinstance(errno, int) else text


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("download cancelled")


def hf_url(endpoint: str, repo: str, filename: str, revision: str = "main") -> str:
    """Build a Hugging Face ``resolve`` URL for a repo file."""
    endpoint = endpoint.rstrip("/")
    return f"{endpoint}/{repo}/resolve/{revision}/{filename}"


def sha256_file(
    path: Path, chunk_bytes: int = 1_048_576, *, cancel: threading.Event | None = None
) -> str:
    """SHA-256 of a file; with ``cancel``, stops between chunks once it is set."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            _check(cancel)
            chunk = fh.read(chunk_bytes)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read(response: http.client.HTTPResponse, size: int) -> bytes:
    """One chunk of the response body; a transport failure is a :class:`DownloadError`."""
    try:
        return response.read(size)
    except (OSError, http.client.HTTPException) as exc:
        raise DownloadError(f"network error reading model ({_describe(exc)})", safe=True) from exc


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

    _check(cancel)  # before any network or file work
    try:
        request = urllib.request.Request(url, headers=headers)
        response = urllib.request.urlopen(request, timeout=30)  # noqa: S310 (http(s) only)
    except (ValueError, http.client.InvalidURL) as exc:
        # The URL was rejected before or while the request was built. Both
        # exceptions quote the URL, so only the type is kept (no chaining).
        raise DownloadError(f"invalid model URL ({type(exc).__name__})", safe=True) from None
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
        raise DownloadError(f"HTTP {int(exc.code)} fetching model", safe=True) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise DownloadError(f"network error fetching model ({_describe(exc)})", safe=True) from exc

    try:
        _check(cancel)
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
            raise DownloadError(f"model exceeds max_model_bytes ({total} > {max_bytes})", safe=True)

        downloaded = resume_from
        mode = "ab" if (resume_from and partial) else "wb"
        with open(part, mode) as fh:
            while True:
                _check(cancel)
                chunk = _read(response, chunk_bytes)
                # A read can block for a long time: whatever it returned (bytes
                # or EOF) is discarded if cancellation arrived meanwhile, so the
                # .part file stays a valid prefix for resume.
                _check(cancel)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if max_bytes and downloaded > max_bytes:
                    raise DownloadError(f"model exceeds max_model_bytes ({max_bytes})", safe=True)
                _check(cancel)
                if progress_cb is not None:
                    progress_cb(downloaded, total)
    finally:
        response.close()

    _check(cancel)
    digest = sha256_file(part, chunk_bytes, cancel=cancel)
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        raise ChecksumMismatch(
            f"checksum mismatch: the file's SHA-256 ({digest}) is not the expected value",
            safe=True,
        )
    _promote(part, dest, cancel)
    return digest


def _promote(part: Path, dest: Path, cancel: threading.Event | None) -> None:
    """The commit point: rename the verified ``.part`` into place unless cancelled."""
    if isinstance(cancel, CancelEvent):
        if not cancel.commit(lambda: part.replace(dest)):
            raise DownloadCancelled("download cancelled")
        return
    _check(cancel)
    part.replace(dest)
