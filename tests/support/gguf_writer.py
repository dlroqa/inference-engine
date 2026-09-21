"""Write a minimal but valid GGUF file for tests (no real weights).

Emits the GGUF magic/header and a handful of metadata key/values — including an
array value — so the header probe can be exercised (scalar extraction + array
skipping) without a multi-GB model.
"""

from __future__ import annotations

import struct
from pathlib import Path

_UINT32, _STRING, _ARRAY = 4, 8, 9


def _gguf_string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _kv_string(key: str, value: str) -> bytes:
    return _gguf_string(key) + struct.pack("<I", _STRING) + _gguf_string(value)


def _kv_uint32(key: str, value: int) -> bytes:
    return _gguf_string(key) + struct.pack("<I", _UINT32) + struct.pack("<I", value)


def _kv_string_array(key: str, values: list[str]) -> bytes:
    body = _gguf_string(key) + struct.pack("<I", _ARRAY)
    body += struct.pack("<I", _STRING) + struct.pack("<Q", len(values))
    for v in values:
        body += _gguf_string(v)
    return body


def write_gguf(
    path: Path,
    *,
    arch: str = "llama",
    name: str = "Tiny Test",
    context_length: int = 2048,
    file_type: int = 15,  # Q4_K_M
    pad_bytes: int = 0,
) -> Path:
    kvs = [
        _kv_string("general.architecture", arch),
        _kv_string("general.name", name),
        _kv_uint32(f"{arch}.context_length", context_length),
        _kv_uint32("general.file_type", file_type),
        # An array value that must be skipped, not materialized, by the probe.
        _kv_string_array("tokenizer.ggml.tokens", ["<a>", "<b>", "<c>"]),
    ]
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header)
        for kv in kvs:
            fh.write(kv)
        if pad_bytes:
            fh.write(b"\0" * pad_bytes)
    return path
