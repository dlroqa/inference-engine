"""Host-compatibility assessment: honest needs_backend / too_large / ok."""

from __future__ import annotations

from engine.models import compat


def test_needs_backend_when_llama_absent(monkeypatch) -> None:
    monkeypatch.setattr(compat, "llama_backend_available", lambda: False)
    report = compat.assess(size_bytes=100)
    assert report.status == compat.Compat.NEEDS_BACKEND
    assert "llama-cpp-python" in report.reason


def test_needs_backend_without_avx(monkeypatch) -> None:
    monkeypatch.setattr(compat, "llama_backend_available", lambda: True)
    monkeypatch.setattr(
        compat.hardware, "summary", lambda: {"cpu_simd_flags": ["sse4_2"], "memory_total": 10**10}
    )
    report = compat.assess(size_bytes=100)
    assert report.status == compat.Compat.NEEDS_BACKEND
    assert "AVX2" in report.reason


def test_too_large(monkeypatch) -> None:
    monkeypatch.setattr(compat, "llama_backend_available", lambda: True)
    monkeypatch.setattr(
        compat.hardware, "summary", lambda: {"cpu_simd_flags": ["avx2"], "memory_total": 1000}
    )
    report = compat.assess(size_bytes=2000)
    assert report.status == compat.Compat.TOO_LARGE


def test_ok(monkeypatch) -> None:
    monkeypatch.setattr(compat, "llama_backend_available", lambda: True)
    monkeypatch.setattr(
        compat.hardware, "summary", lambda: {"cpu_simd_flags": ["avx2"], "memory_total": 10**10}
    )
    report = compat.assess(size_bytes=1000)
    assert report.status == compat.Compat.OK


def test_unknown_when_not_gguf() -> None:
    report = compat.assess(size_bytes=None, valid_gguf=False)
    assert report.status == compat.Compat.UNKNOWN
