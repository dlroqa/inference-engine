"""Internal generation type validation and timings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.inference.types import GenerationRequest, GenerationTimings


def test_request_defaults() -> None:
    req = GenerationRequest(prompt="hi")
    assert req.max_tokens == 256
    assert 0.0 <= req.temperature <= 2.0
    assert req.stop == []


def test_request_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(prompt="hi", bogus=1)  # type: ignore[call-arg]


def test_request_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(prompt="hi", temperature=9.0)
    with pytest.raises(ValidationError):
        GenerationRequest(prompt="hi", max_tokens=0)


def test_timings_properties() -> None:
    t = GenerationTimings(started_at=1.0)
    assert t.ttft_ms is None
    assert t.total_ms is None
    t.first_token_at = 1.2
    t.finished_at = 2.0
    assert round(t.ttft_ms, 1) == 200.0
    assert round(t.total_ms, 1) == 1000.0
