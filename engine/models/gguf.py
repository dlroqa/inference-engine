"""Minimal GGUF header reader — probe model metadata without loading it.

Reads the GGUF magic, version, and the scalar metadata key/values we care about
(architecture, name, context length, quant/file type). Metadata *arrays* (e.g. the
tokenizer vocabulary) are skipped without materializing them, so probing a
multi-GB model reads only a small prefix.

This lets the registry show arch/quant/context and do host-compatibility checks
before anything is loaded. If the file is not a valid GGUF, :func:`probe` returns
``GgufInfo(valid=False)`` rather than raising.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

GGUF_MAGIC = b"GGUF"

# GGUF metadata value type enum.
_UINT8, _INT8, _UINT16, _INT16, _UINT32, _INT32, _FLOAT32, _BOOL = range(8)
_STRING, _ARRAY, _UINT64, _INT64, _FLOAT64 = 8, 9, 10, 11, 12

# Fixed-width scalar type -> (struct format, size).
_SCALAR = {
    _UINT8: ("<B", 1),
    _INT8: ("<b", 1),
    _UINT16: ("<H", 2),
    _INT16: ("<h", 2),
    _UINT32: ("<I", 4),
    _INT32: ("<i", 4),
    _FLOAT32: ("<f", 4),
    _BOOL: ("<?", 1),
    _UINT64: ("<Q", 8),
    _INT64: ("<q", 8),
    _FLOAT64: ("<d", 8),
}

# llama.cpp file_type (LLAMA_FTYPE) -> quant label, for the common cases.
_FTYPE_QUANT = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    7: "Q8_0",
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
    19: "IQ2_XXS",
    20: "IQ2_XS",
    23: "IQ3_XXS",
}

# Matches GGUF quant labels in filenames: K-quants (Q4_K_M), legacy (Q8_0, Q4_1),
# IQ-quants (IQ3_XXS), and full-precision (F16/F32/BF16).
_QUANT_FROM_NAME = re.compile(
    r"(IQ\d+(?:_[0-9A-Za-z]+)*|Q\d+(?:_[0-9A-Za-z]+)*|BF16|F16|F32)", re.IGNORECASE
)


@dataclass(slots=True)
class GgufInfo:
    valid: bool
    arch: str | None = None
    name: str | None = None
    quant: str | None = None
    context_length: int | None = None


def quant_from_filename(filename: str) -> str | None:
    """Best-effort quant label from a GGUF filename (e.g. ``…Q4_K_M.gguf``)."""
    matches = _QUANT_FROM_NAME.findall(filename)
    return matches[-1].upper() if matches else None


def _read(fh: BinaryIO, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise EOFError("unexpected end of GGUF header")
    return data


def _read_scalar(fh: BinaryIO, vtype: int) -> object:
    fmt, size = _SCALAR[vtype]
    return struct.unpack(fmt, _read(fh, size))[0]


def _read_string(fh: BinaryIO) -> str:
    (length,) = struct.unpack("<Q", _read(fh, 8))
    if length > 64 * 1024 * 1024:  # guard against a corrupt/huge length
        raise ValueError("implausible GGUF string length")
    return _read(fh, length).decode("utf-8", errors="replace")


def _skip_value(fh: BinaryIO, vtype: int) -> None:
    """Advance past a value we don't need (notably large arrays)."""
    if vtype in _SCALAR:
        fh.seek(_SCALAR[vtype][1], 1)
    elif vtype == _STRING:
        (length,) = struct.unpack("<Q", _read(fh, 8))
        fh.seek(length, 1)
    elif vtype == _ARRAY:
        _skip_array(fh)
    else:
        raise ValueError(f"unknown GGUF value type {vtype}")


def _skip_array(fh: BinaryIO) -> None:
    (elem_type,) = struct.unpack("<I", _read(fh, 4))
    (count,) = struct.unpack("<Q", _read(fh, 8))
    if elem_type in _SCALAR:
        fh.seek(_SCALAR[elem_type][1] * count, 1)
    elif elem_type == _STRING:
        for _ in range(count):
            (length,) = struct.unpack("<Q", _read(fh, 8))
            fh.seek(length, 1)
    else:
        raise ValueError(f"unsupported GGUF array element type {elem_type}")


def _read_scalar_value(fh: BinaryIO, vtype: int) -> object | None:
    """Read a scalar/string value we want; skip and return None otherwise."""
    if vtype in _SCALAR:
        return _read_scalar(fh, vtype)
    if vtype == _STRING:
        return _read_string(fh)
    _skip_value(fh, vtype)
    return None


def probe(path: Path) -> GgufInfo:
    """Read GGUF header metadata. Never raises on malformed input."""
    try:
        with open(path, "rb") as fh:
            if _read(fh, 4) != GGUF_MAGIC:
                return GgufInfo(valid=False)
            (_version,) = struct.unpack("<I", _read(fh, 4))
            struct.unpack("<Q", _read(fh, 8))  # tensor_count (unused)
            (kv_count,) = struct.unpack("<Q", _read(fh, 8))

            wanted: dict[str, object] = {}
            keys_of_interest = ("general.architecture", "general.name", "general.file_type")
            for _ in range(kv_count):
                key = _read_string(fh)
                (vtype,) = struct.unpack("<I", _read(fh, 4))
                if key in keys_of_interest or key.endswith(".context_length"):
                    value = _read_scalar_value(fh, vtype)
                    if value is not None:
                        wanted[key] = value
                else:
                    _skip_value(fh, vtype)

            arch = wanted.get("general.architecture")
            arch_str = str(arch) if arch is not None else None
            ctx_key = f"{arch_str}.context_length" if arch_str else None
            ctx = wanted.get(ctx_key) if ctx_key else None
            if ctx is None:
                ctx = next(
                    (v for k, v in wanted.items() if k.endswith(".context_length")),
                    None,
                )
            file_type = wanted.get("general.file_type")
            quant = _FTYPE_QUANT.get(int(file_type)) if isinstance(file_type, int) else None
            if quant is None:
                quant = quant_from_filename(Path(path).name)

            name = wanted.get("general.name")
            return GgufInfo(
                valid=True,
                arch=arch_str,
                name=str(name) if name is not None else None,
                quant=quant,
                context_length=int(ctx) if isinstance(ctx, int) else None,
            )
    except (OSError, EOFError, ValueError, struct.error):
        return GgufInfo(valid=False)
