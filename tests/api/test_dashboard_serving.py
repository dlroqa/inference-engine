"""The built SPA is served under /dashboard when present.

The dashboard is a build artifact (``engine/static``) not committed to the repo,
so this test is skipped when it has not been built (e.g. the CI unit-test job).
When it *is* present (local dev, the packaging job), the engine must serve it and
redirect the root there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import engine.main as main
from engine.config import Settings
from engine.main import create_app

_STATIC_INDEX = Path(main.__file__).parent / "static" / "index.html"
_needs_build = pytest.mark.skipif(
    not _STATIC_INDEX.is_file(), reason="dashboard SPA not built (engine/static absent)"
)


@_needs_build
def test_dashboard_index_served(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        resp = c.get("/dashboard/")
        assert resp.status_code == 200
        assert '<div id="root">' in resp.text


@_needs_build
def test_root_redirects_to_dashboard(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        resp = c.get("/", follow_redirects=False)
        assert resp.status_code in (307, 308)
        assert resp.headers["location"] == "/dashboard/"
