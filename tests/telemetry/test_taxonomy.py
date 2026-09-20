"""Error taxonomy: each failure maps to the expected category."""

from __future__ import annotations

import asyncio

from engine.inference.types import (
    BackendBusyError,
    BackendNotReadyError,
    GenerationFailedError,
    ModelLoadError,
)
from engine.telemetry.taxonomy import ErrorCategory, classify


def test_classify_by_code_wins() -> None:
    assert classify(code="quota_exceeded", status_code=429) is ErrorCategory.LIMIT
    assert classify(code="missing_api_key", status_code=401) is ErrorCategory.AUTH
    assert classify(code="model_not_found", status_code=404) is ErrorCategory.MODEL


def test_classify_by_exception_type() -> None:
    assert classify(BackendNotReadyError()) is ErrorCategory.MODEL
    assert classify(ModelLoadError()) is ErrorCategory.MODEL
    assert classify(BackendBusyError()) is ErrorCategory.BACKEND
    assert classify(GenerationFailedError("boom")) is ErrorCategory.BACKEND
    assert classify(asyncio.CancelledError()) is ErrorCategory.CANCELLATION


def test_classify_by_status() -> None:
    assert classify(status_code=400) is ErrorCategory.VALIDATION
    assert classify(status_code=401) is ErrorCategory.AUTH
    assert classify(status_code=413) is ErrorCategory.LIMIT
    assert classify(status_code=429) is ErrorCategory.LIMIT
    assert classify(status_code=503) is ErrorCategory.MODEL
    assert classify(status_code=500) is ErrorCategory.INTERNAL


def test_classify_default_is_internal() -> None:
    assert classify(RuntimeError("?")) is ErrorCategory.INTERNAL
    assert classify() is ErrorCategory.INTERNAL
