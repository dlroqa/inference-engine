"""Reproducible benchmark harness + comparison gate (Block 12.2b).

Drives the engine through its public OpenAI-compatible HTTP API, reports the
Block 12 RFC §8 metric set from honest client-side measurements (plus optional
aggregate route-snapshot deltas), and implements the pure §8.1/§8.2 comparison
gate that Block 12.3 must clear before a routing rule may be enabled.

Measurement/evaluation infrastructure only — it changes no engine behavior. Real
vLLM/SGLang numbers are an operator task on target GPU hardware; CI proves the
harness and gate with fakes and fixtures. See ``docs/benchmarks.md``.
"""

from __future__ import annotations

HARNESS_VERSION = "1.0.0"
SCHEMA_VERSION = 1

__all__ = ["HARNESS_VERSION", "SCHEMA_VERSION"]
