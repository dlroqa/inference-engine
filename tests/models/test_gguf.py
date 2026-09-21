"""GGUF header probe: metadata extraction, array skipping, and bad input."""

from __future__ import annotations

from pathlib import Path

from engine.models.gguf import probe, quant_from_filename
from tests.support.gguf_writer import write_gguf


def test_probe_extracts_metadata(tmp_path: Path) -> None:
    path = write_gguf(
        tmp_path / "m.gguf", arch="llama", name="Tiny", context_length=4096, file_type=15
    )
    info = probe(path)
    assert info.valid
    assert info.arch == "llama"
    assert info.name == "Tiny"
    assert info.context_length == 4096
    assert info.quant == "Q4_K_M"


def test_probe_handles_other_file_type(tmp_path: Path) -> None:
    path = write_gguf(tmp_path / "m.gguf", file_type=7)  # Q8_0
    assert probe(path).quant == "Q8_0"


def test_probe_rejects_non_gguf(tmp_path: Path) -> None:
    bad = tmp_path / "not.gguf"
    bad.write_bytes(b"this is not a gguf file")
    assert probe(bad).valid is False


def test_probe_missing_file(tmp_path: Path) -> None:
    assert probe(tmp_path / "nope.gguf").valid is False


def test_quant_from_filename() -> None:
    assert quant_from_filename("SmolLM2-135M-Instruct-Q4_K_M.gguf") == "Q4_K_M"
    assert quant_from_filename("model-Q8_0.gguf") == "Q8_0"
    assert quant_from_filename("model.gguf") is None
