"""Deterministic, reproducible workload generation (Block 12.2b) — pure module.

A :class:`Workload` is a serializable *manifest* (name, version, seed, exact
per-slice counts, concurrency schedule, warm-up, model, limits, and safe shape
parameters) plus a deterministic :meth:`Workload.generate` that expands the seed
into :class:`RequestSpec` runtime objects. Prompts are synthesised from the seed
and are **never** serialised into a report — only the safe manifest is.

The RFC §5 mix (shares total 100%):

===================  =====
Slice                Share
===================  =====
unique                40%
repeated_prefix       20%
multi_turn            15%
long_context          10%
structured            10%
cancellation           3%
saturation             2%
===================  =====

Exact request counts are allocated from the shares by the largest-remainder
method with a documented tie-break (larger remainder first, then declaration
order), so a given ``(total, seed)`` is fully reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Declaration order is the tie-break for largest-remainder allocation, so keep it
# stable. Shares are the RFC §5 mix and sum to 1.0.
SLICE_SHARES: dict[str, float] = {
    "unique": 0.40,
    "repeated_prefix": 0.20,
    "multi_turn": 0.15,
    "long_context": 0.10,
    "structured": 0.10,
    "cancellation": 0.03,
    "saturation": 0.02,
}

# A fixed, seed-independent lorem base so repeated-prefix requests share the exact
# same leading characters (what the prefix-affinity implementation keys on).
_PREFIX_BASE = (
    "You are a helpful assistant. Consider the following shared context carefully "
    "before answering the question that follows it. " * 16
)


@dataclass(frozen=True)
class RequestSpec:
    """One runtime request. Not serialised into reports (it carries prompt text)."""

    slice: str
    model: str
    messages: tuple[tuple[str, str], ...]  # (role, content) pairs
    stream: bool
    max_tokens: int
    response_format: dict[str, object] | None = None
    #: Observed *content chunks* after which the client cancels (not tokens).
    cancel_after_chunks: int | None = None
    #: "main" runs at the base concurrency; "saturation" runs as a burst phase.
    phase: str = "main"


@dataclass(frozen=True)
class Workload:
    """A reproducible, serialisable benchmark workload manifest."""

    name: str
    version: str
    seed: int
    model: str
    counts: dict[str, int]
    concurrency: int
    burst_concurrency: int
    warmup_requests: int
    max_tokens: int
    prefix_chars: int
    long_context_chars: int
    structured_supported: bool
    stream_mode: str = "all"  # every slice driven as SSE to measure first-content TTFT

    # -- manifest (safe: no prompts, ids, or secrets) ----------------------

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "seed": self.seed,
            "model": self.model,
            "counts": dict(sorted(self.counts.items())),
            "total_requests": sum(self.counts.values()),
            "concurrency": self.concurrency,
            "burst_concurrency": self.burst_concurrency,
            "warmup_requests": self.warmup_requests,
            "max_tokens": self.max_tokens,
            "prefix_chars": self.prefix_chars,
            "long_context_chars": self.long_context_chars,
            "structured_supported": self.structured_supported,
            "stream_mode": self.stream_mode,
        }

    # -- deterministic expansion -------------------------------------------

    def generate(self) -> list[RequestSpec]:
        """Expand the manifest into runtime specs, deterministically from the seed."""
        rng = random.Random(self.seed)
        specs: list[RequestSpec] = []
        for slice_name in SLICE_SHARES:  # stable order
            for i in range(self.counts.get(slice_name, 0)):
                specs.append(self._spec(slice_name, i, rng))
        return specs

    def _spec(self, slice_name: str, i: int, rng: random.Random) -> RequestSpec:
        if slice_name == "unique":
            text = f"Unique request {i}: {_filler(rng, 12)} Answer briefly."
            return RequestSpec(slice_name, self.model, (("user", text),), True, self.max_tokens)
        if slice_name == "repeated_prefix":
            prefix = _PREFIX_BASE[: self.prefix_chars]
            text = f"{prefix} Question {i}: {_filler(rng, 6)}"
            return RequestSpec(slice_name, self.model, (("user", text),), True, self.max_tokens)
        if slice_name == "multi_turn":
            # Conversation *history* only. This does NOT exercise session-ID
            # affinity — no such routing input exists (see module docstring).
            msgs = (
                ("user", f"Turn 1 of dialog {i}: {_filler(rng, 6)}"),
                ("assistant", f"Acknowledged: {_filler(rng, 5)}"),
                ("user", f"Turn 2, follow-up: {_filler(rng, 6)}"),
            )
            return RequestSpec(slice_name, self.model, msgs, True, self.max_tokens)
        if slice_name == "long_context":
            body = _filler(rng, max(1, self.long_context_chars // 6))[: self.long_context_chars]
            text = f"Summarise the following:\n{body}"
            return RequestSpec(slice_name, self.model, (("user", text),), True, self.max_tokens)
        if slice_name == "structured":
            text = f"Return a JSON object describing item {i}: {_filler(rng, 6)}"
            fmt: dict[str, object] = {"type": "json_object"}
            return RequestSpec(
                slice_name,
                self.model,
                (("user", text),),
                True,
                self.max_tokens,
                response_format=fmt,
            )
        if slice_name == "cancellation":
            text = f"Write a long essay {i}: {_filler(rng, 10)}"
            return RequestSpec(
                slice_name,
                self.model,
                (("user", text),),
                True,
                self.max_tokens,
                cancel_after_chunks=1,
            )
        if slice_name == "saturation":
            text = f"Saturation probe {i}: {_filler(rng, 8)}"
            return RequestSpec(
                slice_name, self.model, (("user", text),), True, self.max_tokens, phase="saturation"
            )
        raise ValueError(
            f"unknown slice {slice_name!r}"
        )  # pragma: no cover - guarded by SLICE_SHARES


_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
    "november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu"
).split()


def _filler(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(max(1, words)))


def allocate(total: int, shares: dict[str, float] = SLICE_SHARES) -> dict[str, int]:
    """Largest-remainder allocation of ``total`` across ``shares`` (sum ≈ 1.0).

    Tie-break: larger fractional remainder first, then declaration order. The
    returned counts sum exactly to ``total``.
    """
    if total < 0:
        raise ValueError("total must be non-negative")
    raw = {name: total * share for name, share in shares.items()}
    floors = {name: int(v) for name, v in raw.items()}
    remainder = total - sum(floors.values())
    order = sorted(shares, key=lambda n: (-(raw[n] - floors[n]), list(shares).index(n)))
    for name in order[:remainder]:
        floors[name] += 1
    return floors


def standard_workload(
    *,
    model: str,
    total_requests: int = 200,
    seed: int = 1234,
    concurrency: int = 8,
    burst_concurrency: int = 64,
    warmup_requests: int = 8,
    max_tokens: int = 32,
    prefix_chars: int = 256,
    long_context_chars: int = 2000,
    structured_supported: bool = False,
) -> Workload:
    """The full RFC §5 mix at ``total_requests``, allocated by largest remainder."""
    return Workload(
        name="standard",
        version="1",
        seed=seed,
        model=model,
        counts=allocate(total_requests),
        concurrency=concurrency,
        burst_concurrency=burst_concurrency,
        warmup_requests=warmup_requests,
        max_tokens=max_tokens,
        prefix_chars=prefix_chars,
        long_context_chars=long_context_chars,
        structured_supported=structured_supported,
    )


def smoke_workload(*, model: str, seed: int = 7) -> Workload:
    """A tiny workload with explicit counts (not misleading % rounding)."""
    counts = {"unique": 2, "repeated_prefix": 2, "cancellation": 1}
    return Workload(
        name="smoke",
        version="1",
        seed=seed,
        model=model,
        counts=counts,
        concurrency=2,
        burst_concurrency=2,
        warmup_requests=0,
        max_tokens=16,
        prefix_chars=64,
        long_context_chars=400,
        structured_supported=False,
    )
