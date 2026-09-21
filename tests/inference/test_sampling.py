"""Validation of the Block 8 sampling + structured-output fields on GenerationRequest."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.inference.types import GenerationRequest


def _req(**kw: object) -> GenerationRequest:
    return GenerationRequest(prompt="hi", **kw)


def test_defaults_are_no_ops() -> None:
    r = _req()
    assert r.min_p == 0.0
    assert r.repeat_penalty == 1.0
    assert r.presence_penalty == 0.0
    assert r.frequency_penalty == 0.0
    assert r.mirostat_mode == 0
    assert r.wants_structured_output is False


def test_sampler_ranges_enforced() -> None:
    with pytest.raises(ValidationError):
        _req(min_p=1.5)
    with pytest.raises(ValidationError):
        _req(presence_penalty=3.0)
    with pytest.raises(ValidationError):
        _req(mirostat_mode=3)
    with pytest.raises(ValidationError):
        _req(repeat_penalty=-1.0)


def test_structured_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        _req(json_object=True, json_schema={"type": "object"})
    with pytest.raises(ValidationError):
        _req(grammar='root ::= "x"', json_object=True)


def test_wants_structured_output_flags() -> None:
    assert _req(json_object=True).wants_structured_output is True
    assert _req(json_schema={"type": "object"}).wants_structured_output is True
    assert _req(grammar='root ::= "x"').wants_structured_output is True
