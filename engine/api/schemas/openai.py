"""OpenAI-compatible request/response schemas.

Compatibility philosophy (revised after real-client testing): to be genuinely
**drop-in**, the request model *ignores* unknown and unsupported-but-benign
optional fields (e.g. ``reasoning_effort``, ``frequency_penalty``, ``user``,
``stream_options``) rather than rejecting them — that is how mature
OpenAI-compatible servers behave, and rejecting them breaks real SDK/agent
clients that always send newer params.

What is still rejected are the features that would produce **silently wrong**
output if ignored — because the caller's contract depends on them:
``tools``/``functions`` (expects tool calls), ``response_format`` json modes
(expects valid JSON), and ``n > 1`` (expects multiple choices). Those get a clear
``400`` pointing at the block where they land. See ``docs/compatibility.md``.
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
    def _reject_json_formats(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # ``{"type": "text"}`` is the OpenAI default and is fine (we produce text);
        # JSON modes require structured-output support we do not have yet.
        if v is not None and v.get("type") not in (None, "text"):
            raise ValueError(
                "structured output (response_format json) is not supported yet (planned: Block 8)"
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
