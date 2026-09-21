"""OpenAI-compatible request/response schemas.

Compatibility philosophy (revised after real-client testing): to be genuinely
**drop-in**, the request model *ignores* unknown and unsupported-but-benign
optional fields (e.g. ``reasoning_effort``, ``frequency_penalty``, ``user``,
``stream_options``) rather than rejecting them — that is how mature
OpenAI-compatible servers behave, and rejecting them breaks real SDK/agent
clients that always send newer params.

What is still rejected are the features that would produce **silently wrong**
output if ignored — because the caller's contract depends on them:
``tools``/``functions`` (expects tool calls) and ``n > 1`` (expects multiple
choices). ``response_format`` JSON modes (``json_object``/``json_schema``) are now
honored via constrained decoding when the backend supports it, and rejected with a
clear ``400`` otherwise (never silently returning unconstrained text). See
``docs/compatibility.md``.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    # Ignore extra message fields (e.g. OpenAI's ``name``) for drop-in tolerance.
    model_config = {"extra": "ignore"}

    # Roles are restricted to the supported set; ``tool``/``function`` roles imply
    # tool-calling we do not support yet, so they are rejected (not silently run).
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    # Unknown/benign optional fields are ignored (drop-in compatibility). The
    # contract-changing fields below are modeled explicitly so they can be
    # rejected clearly rather than silently dropped.
    model_config = {"extra": "ignore"}

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    # OpenAI accepts both; max_completion_tokens takes precedence when set.
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32768)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    stop: str | list[str] | None = None
    seed: int | None = None
    n: int = Field(default=1, ge=1)

    # Modeled only to reject them honestly (ignoring these would mislead callers).
    tools: list[Any] | None = None
    functions: list[Any] | None = None
    response_format: dict[str, Any] | None = None

    @field_validator("n")
    @classmethod
    def _only_single_choice(cls, v: int) -> int:
        if v != 1:
            raise ValueError("only n=1 is supported (multiple choices are not implemented)")
        return v

    @field_validator("tools", "functions")
    @classmethod
    def _reject_tools(cls, v: list[Any] | None) -> list[Any] | None:
        if v:
            raise ValueError("tool/function calling is not supported yet (planned: Block 12)")
        return v

    @field_validator("response_format")
    @classmethod
    def _validate_response_format(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # Accept the supported types; an unknown type is rejected rather than
        # silently ignored (the caller depends on the output shape). Whether a JSON
        # mode can actually be honored is a backend-capability check at the edge.
        if v is not None and v.get("type") not in (None, "text", "json_object", "json_schema"):
            raise ValueError(
                f"unsupported response_format type {v.get('type')!r} "
                "(supported: text, json_object, json_schema)"
            )
        return v

    @field_validator("stop")
    @classmethod
    def _normalize_stop(cls, v: str | list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [v] if isinstance(v, str) else v

    def effective_max_tokens(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens

    def stop_list(self) -> list[str]:
        return self.stop if isinstance(self.stop, list) else []

    def structured_output(self) -> tuple[str, dict[str, Any] | None]:
        """Return ``(mode, schema)`` for the requested output constraint.

        ``mode`` is ``"none"``, ``"json_object"``, or ``"json_schema"``; ``schema``
        is the JSON schema for ``json_schema`` mode (else ``None``).
        """
        rf = self.response_format
        if not rf:
            return ("none", None)
        rf_type = rf.get("type")
        if rf_type in (None, "text"):
            return ("none", None)
        if rf_type == "json_object":
            return ("json_object", None)
        # json_schema: OpenAI nests the schema under response_format.json_schema.schema
        nested = rf.get("json_schema") or {}
        schema = nested.get("schema") if isinstance(nested, dict) else None
        return ("json_schema", schema)


# --- Responses ------------------------------------------------------------


def _now() -> int:
    return int(time.time())


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str | None


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=_now)
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage


class Delta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: Delta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=_now)
    model: str
    choices: list[ChatCompletionChunkChoice]


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=_now)
    owned_by: str = "inference-engine"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]
