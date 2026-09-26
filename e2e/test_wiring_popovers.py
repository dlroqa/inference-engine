"""Wiring ("How this works") popovers in a real browser: hover, keyboard, touch, Escape.

The route and backend function each popover names are checked against the
generated route inventory, and the route is called on the running engine, so a
popover is proven to describe a real, reachable endpoint.

Screenshots of each popover are written to ``IE_E2E_SCREENSHOT_DIR`` (uploaded as
a CI artifact for review). Each one is clipped to the control and its popover,
which show only registry text; the test asserts no key material is on the page.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from e2e.conftest import Engine, login

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads(
    (ROOT / "dashboard" / "src" / "lib" / "routes.generated.json").read_text(encoding="utf-8")
)["routes"]
SHOTS = Path(os.environ.get("IE_E2E_SCREENSHOT_DIR", "e2e-screenshots"))


def _handler(method: str, path: str) -> str:
    row = next(r for r in INVENTORY if r["method"] == method and r["path"] == path)
    return f"{row['module']}.{row['handler']}"


def _info(page: Page, name: str) -> Locator:
    return page.get_by_role("button", name=f"How this works: {name}", exact=True)


def _popover(page: Page) -> Locator:
    return page.get_by_role("group", name="How this works:")


def _shot(page: Page, name: str, trigger: Locator, pop: Locator, engine: Engine) -> None:
    assert engine.operator_key not in page.content()
    a = trigger.bounding_box()
    b = pop.bounding_box()
    assert a and b
    vp = page.viewport_size or {"width": 1280, "height": 900}
    pad = 12
    x0 = max(0, min(a["x"], b["x"]) - pad)
    y0 = max(0, min(a["y"], b["y"]) - pad)
    x1 = min(vp["width"], max(a["x"] + a["width"], b["x"] + b["width"]) + pad)
    y1 = min(vp["height"], max(a["y"] + a["height"], b["y"] + b["height"]) + pad)
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(SHOTS / f"{name}.png"),
        clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
    )


@pytest.fixture
def phone(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    pg = context.new_page()
    pg.set_default_timeout(20_000)
    try:
        yield pg
    finally:
        context.close()


def test_hover_shows_route_and_backend_function(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="API keys").click()
    trigger = _info(page, "Create key")
    trigger.hover()
    pop = _popover(page)
    expect(pop).to_be_visible()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    expect(pop).to_contain_text("Backend call")
    expect(pop).to_contain_text("POST /admin/keys")
    expect(pop).to_contain_text(_handler("POST", "/admin/keys"))
    expect(pop).to_contain_text("Operator key")
    _shot(page, "01-hover-create-key", trigger, pop, engine)
    # The route the popover names is live on this engine.
    with engine.api(engine.operator_key) as c:
        assert c.get("/admin/keys").status_code == 200
    # Moving the pointer onto the popover keeps it open; leaving closes it.
    pop.hover()
    page.wait_for_timeout(400)
    expect(pop).to_be_visible()
    page.get_by_role("heading", name="API keys").hover()
    expect(pop).to_be_hidden()


def test_hover_download_shows_kill_switch(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="Models").click()
    trigger = _info(page, "Download model")
    trigger.hover()
    pop = _popover(page)
    expect(pop).to_contain_text("POST /admin/models/download")
    expect(pop).to_contain_text(_handler("POST", "/admin/models/download"))
    expect(pop).to_contain_text("allow_network_downloads")
    _shot(page, "02-hover-download-model", trigger, pop, engine)


def test_keyboard_focus_opens_and_escape_closes(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="Models").click()
    expect(page.get_by_role("heading", name="Models")).to_be_visible()
    # The last control in the sidebar, then Tab: the first control in the page.
    page.get_by_role("button", name="Forget key").focus()
    page.keyboard.press("Tab")
    trigger = _info(page, "Models")
    expect(trigger).to_be_focused()
    pop = _popover(page)
    expect(pop).to_be_visible()
    expect(pop).to_contain_text("GET /admin/models")
    expect(pop).to_contain_text(_handler("GET", "/admin/models"))
    _shot(page, "03-keyboard-focus-models", trigger, pop, engine)
    page.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    expect(trigger).to_be_focused()
    expect(trigger).to_have_attribute("aria-expanded", "false")
    # Enter opens it again and pins it; Tab away closes it.
    page.keyboard.press("Enter")
    expect(pop).to_be_visible()
    page.keyboard.press("Tab")
    expect(pop).to_be_hidden()


def test_touch_tap_opens_and_outside_tap_closes(phone: Page, engine: Engine) -> None:
    login(phone, engine)
    phone.goto(f"{engine.dashboard}#/logs")
    expect(phone.get_by_role("heading", name="Logs")).to_be_visible()
    trigger = _info(phone, "Text filter (no backend call)")
    trigger.tap()
    pop = _popover(phone)
    expect(pop).to_be_visible()
    expect(pop).to_contain_text("No backend call")
    expect(pop).to_contain_text("No request is sent to the engine")
    expect(pop).not_to_contain_text("Backend function")
    # Stays inside the viewport at phone width.
    box = pop.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
    _shot(phone, "04-touch-no-backend-call", trigger, pop, engine)
    phone.get_by_role("heading", name="Logs").tap()
    expect(pop).to_be_hidden()


def test_touch_tap_wired_control_on_phone(phone: Page, engine: Engine) -> None:
    login(phone, engine)
    phone.goto(f"{engine.dashboard}#/logs")
    trigger = _info(phone, "Level filter")
    trigger.tap()
    pop = _popover(phone)
    expect(pop).to_contain_text("GET /logs")
    expect(pop).to_contain_text(_handler("GET", "/logs"))
    _shot(phone, "05-touch-level-filter", trigger, pop, engine)
    phone.keyboard.press("Escape")
    expect(pop).to_be_hidden()
