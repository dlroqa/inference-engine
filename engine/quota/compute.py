"""The compute-unit (CU) cost model.

CU measures work, not message count, so long or long-context requests cost more.
Block 3 keeps it linear in tokens with configurable weights and a per-request
model multiplier (defaults to 1.0); the same model feeds later metered billing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(slots=True)
class ComputeModel:
    prompt_weight: float = 1.0
    completion_weight: float = 1.0

    def actual(self, prompt_tokens: int, completion_tokens: int, multiplier: float = 1.0) -> float:
        return (
            prompt_tokens * self.prompt_weight + completion_tokens * self.completion_weight
        ) * multiplier

    def estimate(self, prompt_tokens: int, max_tokens: int, multiplier: float = 1.0) -> float:
        """Pre-request estimate: prompt tokens plus the requested output cap."""
        return self.actual(prompt_tokens, max_tokens, multiplier)


def estimate_prompt_tokens(text: str) -> int:
    """Cheap pre-request prompt-token estimate (~4 chars/token)."""
    return max(1, math.ceil(len(text) / 4))
