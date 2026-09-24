"""Workload generation: reproducibility, allocation, and safe shapes (Block 12.2b)."""

from __future__ import annotations

import json

from engine.bench.workloads import (
    SLICE_SHARES,
    allocate,
    smoke_workload,
    standard_workload,
)


def test_shares_sum_to_one() -> None:
    assert round(sum(SLICE_SHARES.values()), 9) == 1.0


def test_allocation_is_exact_and_largest_remainder() -> None:
    counts = allocate(200)
    assert sum(counts.values()) == 200
    assert counts == {
        "unique": 80,
        "repeated_prefix": 40,
        "multi_turn": 30,
        "long_context": 20,
        "structured": 20,
        "cancellation": 6,
        "saturation": 4,
    }
    # Sums exactly even when shares do not divide evenly.
    assert sum(allocate(37).values()) == 37
    assert sum(allocate(0).values()) == 0


def test_generation_is_reproducible_from_seed() -> None:
    a = standard_workload(model="m", total_requests=50, seed=99).generate()
    b = standard_workload(model="m", total_requests=50, seed=99).generate()
    assert [s.messages for s in a] == [s.messages for s in b]
    # A different seed changes the synthesised prompts.
    c = standard_workload(model="m", total_requests=50, seed=100).generate()
    assert [s.messages for s in a] != [s.messages for s in c]


def test_every_declared_shape_is_present_and_well_formed() -> None:
    w = standard_workload(model="m", total_requests=200, prefix_chars=128, long_context_chars=1500)
    by_slice: dict[str, list] = {}
    for s in w.generate():
        by_slice.setdefault(s.slice, []).append(s)

    # Repeated-prefix requests share the exact leading prefix_chars characters.
    rp = by_slice["repeated_prefix"]
    prefixes = {s.messages[0][1][:128] for s in rp}
    assert len(prefixes) == 1

    # Multi-turn models conversation history (no session-ID affinity claim).
    mt = by_slice["multi_turn"][0]
    assert [role for role, _ in mt.messages] == ["user", "assistant", "user"]

    # Long context respects the configured size.
    lc = by_slice["long_context"][0]
    assert len(lc.messages[0][1]) >= 1000

    # Structured requests carry a valid response_format.
    st = by_slice["structured"][0]
    assert st.response_format == {"type": "json_object"}

    # Cancellation defines a content-chunk cutoff; saturation is its own phase.
    assert by_slice["cancellation"][0].cancel_after_chunks == 1
    assert all(s.phase == "saturation" for s in by_slice["saturation"])
    assert all(s.phase == "main" for s in by_slice["unique"])


def test_manifest_is_safe_and_serializable() -> None:
    w = standard_workload(model="secret-model", total_requests=20, seed=5)
    manifest = w.manifest()
    blob = json.dumps(manifest)  # must be JSON-serializable
    # No generated prompt text leaks into the manifest.
    for spec in w.generate():
        for _role, content in spec.messages:
            assert content not in blob
    assert manifest["counts"] and manifest["total_requests"] == sum(manifest["counts"].values())


def test_smoke_workload_uses_explicit_counts() -> None:
    w = smoke_workload(model="m")
    assert w.name == "smoke"
    assert sum(w.counts.values()) == len(w.generate())
    assert w.counts == {"unique": 2, "repeated_prefix": 2, "cancellation": 1}
