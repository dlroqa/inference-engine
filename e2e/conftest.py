"""Real-browser acceptance for the operator dashboard (GitHub Actions only).

These tests drive the *built* dashboard in Chromium against a *running* engine
with a real, checksum-verified GGUF model. They are deliberately outside
``tests/`` (not collected by the unit suite) and never skip: a missing
environment variable is a hard failure, so the CI job cannot pass vacuously.

Required environment:

- ``IE_E2E_BASE_URL``      e.g. ``http://127.0.0.1:8140``
- ``IE_E2E_OPERATOR_KEY``  an operator key created with the CLI before start
- ``IE_E2E_MODEL_PATH``    path to the checksum-verified GGUF fixture
- ``IE_E2E_RESTRICTED_BASE_URL`` / ``IE_E2E_RESTRICTED_OPERATOR_KEY``  a second
  engine started with ``allow_model_management`` and ``allow_network_downloads``
  off, seeded beforehand with one loaded and one ready model
  (``e2e/seed_restricted.py``). Used by ``test_switch_states.py``.

Secrets hygiene: keys are read from the environment and never printed; no
traces or videos are recorded. Screenshots are taken only through
``safe_screenshot`` (or after ``assert_no_secrets``), which first checks that no
key appears in the page text or in any form field value.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, sync_playwright


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be set for the browser acceptance suite", pytrace=False)
    return value


@dataclass(frozen=True)
class Engine:
    base: str
    operator_key: str
    client_key: str
    model_path: str

    def api(self, key: str | None = None) -> httpx.Client:
        headers = {"authorization": f"Bearer {key}"} if key else {}
        return httpx.Client(base_url=self.base, headers=headers, timeout=120.0)

    @property
    def dashboard(self) -> str:
        return f"{self.base}/dashboard/"


@pytest.fixture(scope="session")
def engine() -> Engine:
    base = _required("IE_E2E_BASE_URL").rstrip("/")
    operator = _required("IE_E2E_OPERATOR_KEY")
    model_path = _required("IE_E2E_MODEL_PATH")
    # A billing client and its key, created through the real admin API.
    with httpx.Client(
        base_url=base, headers={"authorization": f"Bearer {operator}"}, timeout=30.0
    ) as c:
        resp = c.post("/admin/billing/clients", json={"email": "e2e@example.com"})
        assert resp.status_code in (200, 201), resp.status_code
        client_id = resp.json()["id"]
        resp = c.post(f"/admin/billing/clients/{client_id}/keys", json={"label": "e2e-client-key"})
        assert resp.status_code in (200, 201), resp.status_code
        client_key = resp.json()["token"]
    return Engine(base=base, operator_key=operator, client_key=client_key, model_path=model_path)


@dataclass(frozen=True)
class RestrictedEngine:
    """An engine whose model-management and download switches are off."""

    base: str
    operator_key: str

    def api(self) -> httpx.Client:
        headers = {"authorization": f"Bearer {self.operator_key}"}
        return httpx.Client(base_url=self.base, headers=headers, timeout=60.0)

    @property
    def dashboard(self) -> str:
        return f"{self.base}/dashboard/"


@pytest.fixture(scope="session")
def restricted() -> RestrictedEngine:
    base = _required("IE_E2E_RESTRICTED_BASE_URL").rstrip("/")
    key = _required("IE_E2E_RESTRICTED_OPERATOR_KEY")
    engine = RestrictedEngine(base=base, operator_key=key)
    with engine.api() as c:
        switches = c.get("/admin/system").json()["switches"]
    # The suite is meaningless against an unrestricted engine: fail, never skip.
    assert switches["allow_model_management"] is False, "restricted engine has model management on"
    assert switches["allow_network_downloads"] is False, "restricted engine has downloads on"
    return engine


SHOTS = Path(os.environ.get("IE_E2E_SCREENSHOT_DIR", "e2e-screenshots"))


def mask(secret: str) -> None:
    """Ask the Actions runner to redact a credential created during a test."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{secret}", flush=True)


@dataclass(frozen=True)
class DisposableKey:
    id: str
    token: str
    label: str


def create_operator_key(engine: Engine, label_prefix: str) -> DisposableKey:
    """A fresh operator key owned by one test, created with the suite owner key."""
    label = f"{label_prefix}-{uuid.uuid4().hex[:8]}"
    with engine.api(engine.operator_key) as c:
        resp = c.post("/admin/keys", json={"label": label})
        assert resp.status_code in (200, 201), resp.status_code
        body = resp.json()
    mask(body["token"])
    return DisposableKey(id=body["id"], token=body["token"], label=label)


def purge_key(engine: Engine, key_id: str) -> None:
    with engine.api(engine.operator_key) as c:
        c.delete(f"/admin/keys/{key_id}")
        c.delete(f"/admin/keys/{key_id}", params={"purge": "true"})


def import_ready_copy(engine: Engine, directory: Path, name_prefix: str) -> dict[str, str]:
    """Import this test's own copy of the fixture and wait until it is ready."""
    name = f"{name_prefix}-{uuid.uuid4().hex[:8]}"
    copy = directory / f"{name}.gguf"
    shutil.copyfile(engine.model_path, copy)
    with engine.api(engine.operator_key) as c:
        resp = c.post("/admin/models/import", json={"path": str(copy), "name": name})
        assert resp.status_code in (200, 201, 202), resp.status_code
        model_id = resp.json()["id"]
        deadline = time.monotonic() + 120
        while True:
            status = c.get(f"/admin/models/{model_id}").json()["status"]
            if status == "ready":
                return {"id": model_id, "name": name}
            assert status not in ("error", "cancelled"), status
            assert time.monotonic() < deadline, "imported fixture copy not ready in 120 s"
            time.sleep(0.5)


def assert_no_secrets(page: Page, secrets: Iterable[str]) -> None:
    """No key is in the page text/markup or in any form field's current value."""
    values = page.eval_on_selector_all("input, textarea", "els => els.map(e => e.value)")
    content = page.content()
    for secret in secrets:
        assert secret, "empty secret"
        assert secret not in content, "a key is present in the page"
        assert all(secret not in v for v in values), "a key is present in a form field"


def safe_screenshot(
    page: Page, name: str, secrets: Iterable[str], *, full_page: bool = False
) -> None:
    """A viewport (or full-page) screenshot, only after the secrets check."""
    secrets = list(secrets)
    assert_no_secrets(page, secrets)
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full_page, animations="disabled")


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    # A fresh context per test: no saved key, no shared localStorage.
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    pg.set_default_timeout(20_000)
    try:
        yield pg
    finally:
        context.close()


def login(page: Page, engine: Engine, key: str | None = None) -> None:
    page.goto(engine.dashboard)
    page.get_by_label("Operator API key").fill(key or engine.operator_key)
    page.get_by_role("button", name="Continue").click()
