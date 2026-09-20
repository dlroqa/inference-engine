"""Structured error taxonomy.

Every failure an operator sees is classified into one of a small, fixed set of
categories so the answer to *"whose fault was it?"* is unambiguous:

- ``validation``   — malformed/unsupported client input (400).
- ``auth``         — missing/invalid credentials (401).
- ``limit``        — rate, concurrency, size, or quota rejection (413/429).
- ``model``        — no model loaded / model-not-found / not-ready (404/503).
- ``backend``      — inference backend execution failure (500/503).
- ``cancellation`` — client disconnected / consumer aborted.
- ``internal``     — anything else (unexpected server error).

``classify`` maps an exception (and, for the HTTP edge, its status code) onto a
category. This is intentionally centralized so logs, the live feed, and the Logs
view all agree on categories.
"""

from __future__ import annotations

import asyncio
import enum

from engine.inference.types import (
    BackendBusyError,
    BackendNotReadyError,
    GenerationFailedError,
    ModelLoadError,
)


class ErrorCategory(enum.StrEnum):
    VALIDATION = "validation"
    AUTH = "auth"
    LIMIT = "limit"
    MODEL = "model"
    BACKEND = "backend"
    CANCELLATION = "cancellation"
    INTERNAL = "internal"


# HTTP-status → category for edge errors that carry a status but not a dedicated
# exception type (e.g. the OpenAI-shaped errors the gateway raises).
_STATUS_CATEGORY = {
    400: ErrorCategory.VALIDATION,
    401: ErrorCategory.AUTH,
    403: ErrorCategory.AUTH,
    404: ErrorCategory.MODEL,
    413: ErrorCategory.LIMIT,
    429: ErrorCategory.LIMIT,
    503: ErrorCategory.MODEL,
}

# Error ``code`` values that pin the category regardless of status.
_CODE_CATEGORY = {
    "missing_api_key": ErrorCategory.AUTH,
    "invalid_api_key": ErrorCategory.AUTH,
    "payload_too_large": ErrorCategory.LIMIT,
    "rate_limit_exceeded": ErrorCategory.LIMIT,
    "concurrency_limit_exceeded": ErrorCategory.LIMIT,
    "quota_exceeded": ErrorCategory.LIMIT,
    "model_not_found": ErrorCategory.MODEL,
    "model_not_loaded": ErrorCategory.MODEL,
    "model_busy": ErrorCategory.BACKEND,
}


def classify(
    exc: BaseException | None = None,
    *,
    status_code: int | None = None,
    code: str | None = None,
) -> ErrorCategory:
    """Classify a failure into an :class:`ErrorCategory`.

    Precedence: explicit error ``code`` → known exception type → HTTP status →
    ``internal``.
    """
    if code and code in _CODE_CATEGORY:
        return _CODE_CATEGORY[code]

    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return ErrorCategory.INTERNAL
    if isinstance(exc, (BackendNotReadyError, ModelLoadError)):
        return ErrorCategory.MODEL
    if isinstance(exc, BackendBusyError):
        return ErrorCategory.BACKEND
    if isinstance(exc, GenerationFailedError):
        return ErrorCategory.BACKEND
    if isinstance(exc, asyncio.CancelledError):
        return ErrorCategory.CANCELLATION

    if status_code is not None and status_code in _STATUS_CATEGORY:
        return _STATUS_CATEGORY[status_code]
    if status_code is not None and 500 <= status_code < 600:
        return ErrorCategory.INTERNAL

    return ErrorCategory.INTERNAL
