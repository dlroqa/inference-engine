"""OpenAI-compatible request/response schemas (deliberately supported subset).

Only the fields the engine actually honors are modeled. ``extra="forbid"`` makes
any unsupported field a clear validation error rather than a silent no-op, per the
Block 2 contract. See ``docs/compatibility.md`` for the published matrix.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    model_config = {"extra": "forbid"}

    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "forbid"}

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

    @field_validator("n")
    @classmethod
    def _only_single_choice(cls, v: int) -> int:
        if v != 1:
            raise ValueError("only n=1 is supported")
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
