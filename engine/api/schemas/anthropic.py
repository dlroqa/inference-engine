"""Anthropic Messages API schemas (supported subset).

Models the request/response shapes for ``POST /v1/messages`` and the streaming
event payloads. Only text content is supported; tool use, tool results, and
image/document content blocks are rejected with a clear error rather than silently
dropped (the caller's contract depends on them). See ``docs/compatibility.md``.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from engine.inference.types import Message


def _extract_text(content: str | list[dict[str, Any]], *, where: str) -> str:
    """Return the concatenated text of an Anthropic content value.

    Raises ``ValueError`` for any non-text block so unsupported multimodal/tool
    content is rejected honestly instead of being silently ignored.
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        btype = block.get("type") if isinstance(block, dict) else None
        if btype != "text":
            raise ValueError(
                f"unsupported content block {btype!r} in {where}; only 'text' is supported"
            )
        parts.append(str(block.get("text", "")))
    return "".join(parts)


class AnthropicMessage(BaseModel):
    model_config = {"extra": "ignore"}

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]

    @field_validator("content")
    @classmethod
    def _text_only(cls, v: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        # Validate now so unsupported content yields a clean 400 (not a 500 later).
        _extract_text(v, where="message")
        return v

    def text(self) -> str:
        return _extract_text(self.content, where=f"{self.role} message")


class MessagesRequest(BaseModel):
    # Unknown/benign optional fields (e.g. ``metadata``) are ignored for drop-in
    # tolerance; contract-changing features are modeled so they can be rejected.
    model_config = {"extra": "ignore"}

    model: str
    messages: list[AnthropicMessage] = Field(min_length=1)
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int = Field(ge=1, le=32768)
    temperature: float = Field(default=1.0, ge=0.0, le=1.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    top_k: int = Field(default=0, ge=0)
    stop_sequences: list[str] = Field(default_factory=list)
    stream: bool = False

    # Modeled only to reject them honestly (ignoring these would mislead callers).
    tools: list[Any] | None = None
    tool_choice: dict[str, Any] | None = None

    @field_validator("tools")
    @classmethod
    def _reject_tools(cls, v: list[Any] | None) -> list[Any] | None:
        if v:
            raise ValueError("tool use is not supported yet (planned: Block 12)")
        return v

    @field_validator("tool_choice")
    @classmethod
    def _reject_tool_choice(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v:
            raise ValueError("tool_choice is not supported yet (planned: Block 12)")
        return v

    @field_validator("system")
    @classmethod
    def _system_text_only(
        cls, v: str | list[dict[str, Any]] | None
    ) -> str | list[dict[str, Any]] | None:
        if v is not None:
            _extract_text(v, where="system prompt")
        return v

    def system_text(self) -> str | None:
        if self.system is None:
            return None
        return _extract_text(self.system, where="system prompt")

    def to_internal_messages(self) -> list[Message]:
        """Flatten to the internal chat messages, folding ``system`` in first."""
        out: list[Message] = []
        system = self.system_text()
        if system:
            out.append(Message(role="system", content=system))
        for m in self.messages:
            out.append(Message(role=m.role, content=m.text()))
        return out

    def prompt_text(self) -> str:
        return "\n".join(m.content for m in self.to_internal_messages())


# --- Non-streaming response ----------------------------------------------


def _msg_id() -> str:
    import uuid

    return f"msg_{uuid.uuid4().hex}"


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class AnthropicUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class MessagesResponse(BaseModel):
    id: str = Field(default_factory=_msg_id)
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: list[TextBlock]
    stop_reason: str | None
    stop_sequence: str | None = None
    usage: AnthropicUsage


def _now() -> int:
    return int(time.time())
