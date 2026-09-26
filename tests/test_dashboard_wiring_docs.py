"""Documentation links in the dashboard's wiring registry point at real pages.

The dashboard links each registry ``docs`` reference to the canonical GitHub copy
(``DOCS_BASE`` in ``dashboard/src/lib/wiring.ts``). Without network access, this
checks that every referenced file exists in the repository and that every heading
fragment matches a heading as GitHub slugifies it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIRING = ROOT / "dashboard" / "src" / "lib" / "wiring.ts"


def _refs() -> list[str]:
    return re.findall(r'ref: "([^"]+)"', WIRING.read_text(encoding="utf-8"))


def _slug(heading: str) -> str:
    # GitHub: lowercase, drop punctuation except hyphens/underscores, spaces -> "-".
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def test_registry_has_docs_references() -> None:
    assert len(_refs()) >= 5


def test_docs_base_is_the_canonical_repository() -> None:
    text = WIRING.read_text(encoding="utf-8")
    assert 'DOCS_BASE = "https://github.com/dlroqa/inference-engine/blob/main/"' in text


@pytest.mark.parametrize("ref", _refs())
def test_docs_reference_resolves(ref: str) -> None:
    path, _, fragment = ref.partition("#")
    page = ROOT / path
    assert page.is_file(), f"{ref}: {path} does not exist"
    if fragment:
        headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", page.read_text(encoding="utf-8"), re.M)
        slugs = {_slug(h) for h in headings}
        assert fragment in slugs, f"{ref}: no heading with slug {fragment!r} in {path}"
