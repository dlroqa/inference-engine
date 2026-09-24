"""Internal, provider- and transport-agnostic generation types.

These types are the stable seam between the API edge (HTTP, OpenAI/Anthropic
dialects — later blocks) and the inference workers. Nothing here depends on
FastAPI or any provider SDK: the same ``GenerationRequest`` and token stream are
used whether the caller is a CLI command, a unit test, or a future HTTP router.
"""

from __future__ import annotations

import enum
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class BackendState(enum.StrEnum):
    """Explicit lifecycle state of an inference backend."""

    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    GENERATING = "generating"
    FAILED = "failed"


class FinishReason(enum.StrEnum):
    """Why a generation ended."""

    STOP = "stop"  # natural end / stop sequence / EOS
    LENGTH = "length"  # hit the max-token cap
    CANCELLED = "cancelled"  # client/consumer aborted
    ERROR = "error"  # backend failure mid-generation


# A token producer is a generator that yields token text and may ``return`` a
# FinishReason to indicate how it ended (defaults to STOP).
TokenGenerator = Generator[str, None, "FinishReason | None"]


class Message(BaseModel):
    """One chat message. Generic (not provider-specific)."""

    model_config = {"extra": "forbid"}

    role: Literal["system", "user", "assistant"]
    content: str


class GenerationRequest(BaseModel):
    """A validated, normalized request for token generation.

    Carries **either** a raw ``prompt`` (completion style) **or** ``messages``
    (chat style); exactly one must be provided. Provider dialects (OpenAI now,
    Anthropic in Block 8) translate onto this single internal request at the API
    edge. The sampler set is deliberately minimal here; the full surface is
    Block 8.
    """

    model_config = {"extra": "forbid"}

    prompt: str | None = None
    messages: list[Message] | None = None
    request_id: str = ""
    max_tokens: int | None = Field(default=256, ge=1, le=32768)
    temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=0)
    seed: int | None = None
    stop: list[str] = Field(default_factory=list)

    # Extended sampling controls (Block 8). Defaults are no-ops so a request that
    # sets none of them behaves exactly as before. Each maps to a llama.cpp
    # sampler the ``llama-cpp-python`` binding exposes; see docs/compatibility.md
    # for the sampler order and per-dialect mapping.
    min_p: float = Field(default=0.0, ge=0.0, le=1.0)
    repeat_penalty: float = Field(default=1.0, ge=0.0, le=2.0)
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    # Mirostat (0 = off, 1 = v1, 2 = v2). When enabled, llama.cpp targets a fixed
    # output perplexity and top_k/top_p/min_p are not applied — this exclusivity is
    # documented, not silently reinterpreted.
    mirostat_mode: int = Field(default=0, ge=0, le=2)
    mirostat_tau: float = Field(default=5.0, ge=0.0)
    mirostat_eta: float = Field(default=0.1, ge=0.0)

    # Structured output (Block 8). At most one of these is set. ``grammar`` is a
    # GBNF grammar; ``json_schema`` constrains output to a JSON schema; ``json_object``
    # requests any valid JSON. Honored only by a backend that reports
    # ``supports_structured_output`` — the edge rejects them otherwise.
    grammar: str | None = None
    json_schema: dict[str, object] | None = None
    json_object: bool = False

    @model_validator(mode="after")
    def _exactly_one_input(self) -> GenerationRequest:
        has_prompt = self.prompt is not None
        has_messages = bool(self.messages)
        if has_prompt == has_messages:
            raise ValueError("provide exactly one of 'prompt' or 'messages'")
        return self

    @model_validator(mode="after")
    def _one_structured_mode(self) -> GenerationRequest:
        modes = [self.grammar is not None, self.json_schema is not None, self.json_object]
        if sum(modes) > 1:
            raise ValueError("set at most one of 'grammar', 'json_schema', or 'json_object'")
        return self

    @property
    def wants_structured_output(self) -> bool:
        return self.grammar is not None or self.json_schema is not None or self.json_object

    @property
    def is_chat(self) -> bool:
        return self.messages is not None

    def prompt_text(self) -> str:
        """A plain-text view of the input (used for token-count estimates)."""
        if self.prompt is not None:
            return self.prompt
        return "\n".join(m.content for m in self.messages or [])


@dataclass(slots=True)
class GenerationChunk:
    """One streamed token/text delta, in order."""

    text: str
    index: int


@dataclass(slots=True)
class GenerationTimings:
    """Basic timings recorded to validate behavior (not a benchmark suite)."""

    started_at: float
    first_token_at: float | None = None
    finished_at: float | None = None

    @property
    def ttft_ms(self) -> float | None:
        if self.first_token_at is None:
            return None
        return (self.first_token_at - self.started_at) * 1000.0

    @property
    def total_ms(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at) * 1000.0


@dataclass(slots=True)
class GenerationResult:
    """Terminal result of a generation."""

    request_id: str
    text: str
    finish_reason: FinishReason
    prompt_tokens: int
    completion_tokens: int
    timings: GenerationTimings


@dataclass(slots=True)
class Capabilities:
    """What a loaded backend can do. Honest and minimal for Block 1."""

    backend: str
    model_id: str
    context_length: int
    streaming: bool = True
    max_output_tokens: int | None = None
    #: Whether the backend can honor grammar/JSON-schema/JSON-object constrained
    #: output. Reported honestly per installed binding; the edge rejects structured
    #: requests when this is False rather than returning unconstrained text.
    supports_structured_output: bool = False
    #: Whether the backend maintains a prompt/prefix cache whose reuse benefits from
    #: routing prefix-sharing requests to the same backend (vLLM/SGLang do; the
    #: local llama.cpp path does not). Gates prefix-affinity routing (Block 10.4).
    supports_prefix_cache: bool = False
    #: Whether the backend can report KV-cache utilization / prefix-cache hit rate
    #: (via its metrics endpoint). Reported honestly; the edge omits a cache block
    #: for backends that cannot (Block 10.4).
    supports_kv_cache_metrics: bool = False
    extra: dict[str, object] = field(default_factory=dict)


# --- Error taxonomy -------------------------------------------------------


class BackendError(Exception):
    """Base class for inference backend errors."""


class ModelLoadError(BackendError):
    """A model failed to load; the backend is left recoverable (unloaded)."""


class BackendNotReadyError(BackendError):
    """A generation was requested while no model is ready."""


class BackendBusyError(BackendError):
    """A generation was requested while another is already in progress."""


class GenerationFailedError(BackendError):
    """A generation failed after it had started streaming."""


class FeatureUnsupportedError(Exception):
    """A request required a feature no ready, model-eligible backend supports.

    Raised by placement (Block 12.1) when a known model has a ready backend but
    none of the model-eligible backends support a required feature (e.g. structured
    output). It is a *request* error (maps to HTTP 400), distinct from
    ``BackendNotReadyError`` (503, nothing ready serves the model) and saturation
    (all feature-capable candidates full). ``features`` names the unmet feature(s).
    """

    def __init__(self, features: frozenset[str], *, model: str | None = None) -> None:
        self.features = features
        self.model = model
        detail = ", ".join(sorted(features)) or "requested feature"
        super().__init__(detail)
