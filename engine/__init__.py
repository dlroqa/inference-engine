"""Inference Engine — Block 0 foundation package.

This package intentionally contains only the foundational skeleton described by
Block 0 of the vertical-slice build: application factory, layered configuration,
SQLite/WAL persistence with migrations, structured logging, and health
endpoints. No inference runtime exists yet (see Block 1).
"""

# Single source of release version truth: pyproject reads it via setuptools
# dynamic metadata, and the release workflow requires the ``vX.Y.Z`` tag to match.
__version__ = "0.1.1"
