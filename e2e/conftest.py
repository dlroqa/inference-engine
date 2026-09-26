"""Real-browser acceptance for the operator dashboard (GitHub Actions only).

These tests drive the *built* dashboard in Chromium against a *running* engine
with a real, checksum-verified GGUF model. They are deliberately outside
``tests/`` (not collected by the unit suite) and never skip: a missing
environment variable is a hard failure, so the CI job cannot pass vacuously.

Required environment:

- ``IE_E2E_BASE_URL``      e.g. ``http://127.0.0.1:8140``
- ``IE_E2E_OPERATOR_KEY``  an operator key created with the CLI before start
- ``IE_E2E_MODEL_PATH``    path to the checksum-verified GGUF fixture

Secrets hygiene: keys are read from the environment and never printed; no
screenshots, traces, or videos are recorded.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

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
