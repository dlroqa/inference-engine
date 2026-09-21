"""Model lifecycle (Block 6): registry, GGUF probe, downloads, host compat.

The registry (:mod:`engine.models.registry`) is the source of truth for which
GGUF models exist, their verified checksums, probed metadata, and which one is
active. :mod:`engine.models.gguf` reads GGUF header metadata without loading a
model; :mod:`engine.models.downloader` fetches models with checksum verification
and resumable progress; :mod:`engine.models.compat` reports host runnability
honestly (``needs_backend`` when llama.cpp/AVX is unavailable); and
:mod:`engine.models.service` orchestrates import/download/load/unload.
"""
