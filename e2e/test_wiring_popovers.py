"""Wiring ("How this works") popovers in a real browser.

Three kinds of claims are kept separate:

- Inventory agreement: the route and backend function a popover names are read
  from the generated route inventory here and compared with the rendered text.
  (Registry-to-inventory drift is also checked by vitest and export_routes.py.)
- Rendered explanation: what the popover shows, where, and how it behaves:
  hover, keyboard, touch, Escape, viewport containment, docs links.
- Action and backend effect: only where a test performs the named request and
  checks its effect on the engine. Here that is Create key (POST /admin/keys).
  Model import/load effects are covered by test_model_import_load_and_generate
  in test_dashboard_browser.py; the download case below checks the explanation
  only and does not start a download.

Screenshots go to ``IE_E2E_SCREENSHOT_DIR`` (uploaded as a CI artifact). Each is
clipped to a control and its popover and taken only after asserting that no
operator key is on the page, and never after a one-time token is shown.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from e2e.conftest import Engine, login

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads(
    (ROOT / "dashboard" / "src" / "lib" / "routes.generated.json").read_text(encoding="utf-8")
)["routes"]
SHOTS = Path(os.environ.get("IE_E2E_SCREENSHOT_DIR", "e2e-screenshots"))
DOCS_BASE = "https://github.com/dlroqa/inference-engine/blob/main/"


def _handler(method: str, path: str) -> str:
    row = next(r for r in INVENTORY if r["method"] == method and r["path"] == path)
    return f"{row['module']}.{row['handler']}"


def _info(page: Page, name: str) -> Locator:
    return page.get_by_role("button", name=f"How this works: {name}", exact=True)


def _popover(page: Page) -> Locator:
    return page.get_by_role("group", name="How this works:")


def _assert_contained(page: Page, pop: Locator) -> None:
    """The popover's box lies inside the viewport on all four edges."""
    box = pop.bounding_box()
    vp = page.viewport_size
    assert box and vp
    eps = 0.5
    assert box["x"] >= -eps and box["y"] >= -eps, box
    assert box["x"] + box["width"] <= vp["width"] + eps, (box, vp)
    assert box["y"] + box["height"] <= vp["height"] + eps, (box, vp)


def _end_is_reachable(pop: Locator) -> None:
    """Scrolled to its end, the panel's last content is inside its visible box."""
    fits = pop.evaluate(
        """el => {
            el.scrollTop = el.scrollHeight;
            const last = el.lastElementChild.lastElementChild.getBoundingClientRect();
            const box = el.getBoundingClientRect();
            return last.bottom <= box.bottom + 1 && last.top >= box.top - 1;
        }"""
    )
    assert fits


def _is_scrollable(pop: Locator) -> bool:
    return bool(pop.evaluate("el => el.scrollHeight > el.clientHeight + 1"))


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
        animations="disabled",
    )


def _phone(browser: Browser, width: int, height: int) -> Iterator[Page]:
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    )
    pg = context.new_page()
    pg.set_default_timeout(20_000)
    try:
        yield pg
    finally:
        context.close()


@pytest.fixture
def phone(browser: Browser) -> Iterator[Page]:
    yield from _phone(browser, 390, 844)


@pytest.fixture
def phone_landscape(browser: Browser) -> Iterator[Page]:
    yield from _phone(browser, 844, 390)


# --- Explanation plus a real action: Create key -----------------------------


def test_create_key_explanation_and_real_post(page: Page, engine: Engine) -> None:
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
    _assert_contained(page, pop)
    _shot(page, "01-hover-create-key", trigger, pop, engine)
    # Moving the pointer onto the popover keeps it open; leaving closes it.
    pop.hover()
    page.wait_for_timeout(400)
    expect(pop).to_be_visible()
    page.get_by_role("heading", name="API keys").hover()
    expect(pop).to_be_hidden()

    # The action the popover describes: the UI sends POST /admin/keys and the
    # engine stores a new operator key. (No screenshots after the token shows.)
    page.get_by_label("Label (optional)").fill("e2e-popover-create")
    with page.expect_response(
        lambda r: r.request.method == "POST" and urlparse(r.url).path == "/admin/keys"
    ) as posted:
        page.get_by_role("button", name="Create key", exact=True).click()
    assert posted.value.status in (200, 201)
    expect(page.get_by_test_id("new-token")).to_be_visible()
    with engine.api(engine.operator_key) as c:
        keys = c.get("/admin/keys").json()["keys"]
        rows = [k for k in keys if k["label"] == "e2e-popover-create"]
        assert len(rows) == 1
        assert rows[0]["role"] == "operator" and rows[0]["revoked"] is False
        # Fixture cleanup: revoke the key again.
        assert c.delete(f"/admin/keys/{rows[0]['id']}").status_code in (200, 204)


# --- Explanation only --------------------------------------------------------


def test_download_explanation_only(page: Page, engine: Engine) -> None:
    """Checks the rendered explanation; it does not start a download."""
    login(page, engine)
    page.get_by_role("link", name="Models").click()
    trigger = _info(page, "Download model")
    trigger.hover()
    pop = _popover(page)
    expect(pop).to_contain_text("POST /admin/models/download")
    expect(pop).to_contain_text(_handler("POST", "/admin/models/download"))
    # Both switches gate a download; either one off disables it.
    expect(pop).to_contain_text("Kill switches")
    expect(pop).to_contain_text("allow_model_management")
    expect(pop).to_contain_text("allow_network_downloads")
    expect(pop).to_contain_text("Both must be on; turning off either one disables this action.")
    # The checksum is conditional on an expected SHA-256, not a blanket guarantee.
    expect(pop).to_contain_text("calculates its SHA-256")
    expect(pop).to_contain_text("When you provide an expected SHA-256, the download is checked")
    expect(pop).not_to_contain_text("checksum-verified")
    _assert_contained(page, pop)
    _shot(page, "02-hover-download-model", trigger, pop, engine)


# --- Keyboard ----------------------------------------------------------------


def test_keyboard_reaches_scroll_content_and_docs_link(page: Page, engine: Engine) -> None:
    # A short viewport makes the Models explanation taller than the space below it.
    page.set_viewport_size({"width": 1280, "height": 300})
    login(page, engine)
    page.get_by_role("link", name="Models").click()
    expect(page.get_by_role("heading", name="Models")).to_be_visible()
    # From the sidebar's last control, Tab reaches the page's first hint.
    page.get_by_role("button", name="Forget key").focus()
    page.keyboard.press("Tab")
    trigger = _info(page, "Models")
    expect(trigger).to_be_focused()
    pop = _popover(page)
    expect(pop).to_be_visible()
    expect(pop).to_contain_text("GET /admin/models")
    expect(pop).to_contain_text(_handler("GET", "/admin/models"))
    _assert_contained(page, pop)
    assert _is_scrollable(pop), "expected a constrained, scrollable panel at 300 px"

    # Tab moves into the panel; the keyboard scrolls it to the end.
    page.keyboard.press("Tab")
    expect(pop).to_be_focused()
    page.keyboard.press("End")
    page.wait_for_function(
        "el => el.scrollTop > 0 && el.scrollTop + el.clientHeight >= el.scrollHeight - 1",
        arg=pop.element_handle(),
    )
    # Tab reaches the docs link, which stays inside the visible panel.
    page.keyboard.press("Tab")
    link = pop.get_by_role("link", name="README: model lifecycle (opens in a new tab)")
    expect(link).to_be_focused()
    expect(link).to_have_attribute("href", f"{DOCS_BASE}README.md#model-lifecycle-block-6")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(pop).to_be_visible()
    _shot(page, "03-keyboard-docs-link", trigger, pop, engine)

    # Escape from inside the panel returns focus to the button and stays closed.
    page.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    expect(trigger).to_be_focused()
    page.wait_for_timeout(400)
    expect(pop).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")

    # Enter reopens it; Tab through the panel and its link, then out, closes it.
    page.keyboard.press("Enter")
    expect(pop).to_be_visible()
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    expect(link).to_be_focused()
    page.keyboard.press("Tab")
    expect(pop).to_be_hidden()


def test_escape_in_dialog_closes_popover_before_dialog(page: Page, engine: Engine) -> None:
    with engine.api(engine.operator_key) as c:
        created = c.post("/admin/keys", json={"label": "e2e-popover-dialog"})
        assert created.status_code in (200, 201)
        key_id = created.json()["id"]
        prefix = created.json()["prefix"]
    login(page, engine)
    page.get_by_role("link", name="API keys").click()
    page.get_by_role("button", name=f"Revoke key {prefix}").click()
    dialog = page.get_by_role("dialog", name="Revoke this key?")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    page.keyboard.press("Tab")  # Revoke key
    page.keyboard.press("Tab")  # the hint, after the actions
    trigger = dialog.get_by_role("button", name="How this works: Revoke key")
    expect(trigger).to_be_focused()
    pop = dialog.get_by_role("group")
    expect(pop).to_be_visible()
    expect(pop).to_contain_text("DELETE /admin/keys/{key_id}")
    expect(pop).to_contain_text(_handler("DELETE", "/admin/keys/{key_id}"))
    expect(pop).to_contain_text("No backend call")  # the Cancel explanation
    _assert_contained(page, pop)
    _shot(page, "04-dialog-revoke-hint", trigger, pop, engine)
    page.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    expect(dialog).to_be_visible()
    expect(trigger).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    with engine.api(engine.operator_key) as c:
        row = next(k for k in c.get("/admin/keys").json()["keys"] if k["id"] == key_id)
        assert row["revoked"] is False
        assert c.delete(f"/admin/keys/{key_id}").status_code in (200, 204)


def test_escape_on_hover_preview_in_dialog_keeps_dialog_and_focus(
    page: Page, engine: Engine
) -> None:
    """Focus stays on Cancel; only the pointer opens the hint."""
    with engine.api(engine.operator_key) as c:
        created = c.post("/admin/keys", json={"label": "e2e-popover-hover-dialog"})
        assert created.status_code in (200, 201)
        key_id = created.json()["id"]
        prefix = created.json()["prefix"]
    login(page, engine)
    page.get_by_role("link", name="API keys").click()
    opener = page.get_by_role("button", name=f"Revoke key {prefix}")
    opener.click()
    dialog = page.get_by_role("dialog", name="Revoke this key?")
    cancel = dialog.get_by_role("button", name="Cancel")
    expect(cancel).to_be_focused()

    trigger = dialog.get_by_role("button", name="How this works: Revoke key")
    trigger.hover()
    pop = dialog.get_by_role("group")
    expect(pop).to_be_visible()
    expect(cancel).to_be_focused()  # hovering did not move focus
    _shot(page, "10-dialog-hover-preview", trigger, pop, engine)

    # First Escape: only the preview closes; the dialog stays and Cancel keeps focus.
    page.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    expect(dialog).to_be_visible()
    expect(cancel).to_be_focused()
    # The pointer is still over the hint: it stays dismissed past the hover delay.
    page.wait_for_timeout(600)
    expect(pop).to_be_hidden()
    expect(dialog).to_be_visible()

    # Second Escape: the dialog cancels and focus returns to its opener.
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    with engine.api(engine.operator_key) as c:
        row = next(k for k in c.get("/admin/keys").json()["keys"] if k["id"] == key_id)
        assert row["revoked"] is False  # nothing was confirmed
        assert c.delete(f"/admin/keys/{key_id}").status_code in (200, 204)


# --- Viewport containment ----------------------------------------------------


def test_centered_trigger_in_short_viewport_stays_inside_all_edges(
    page: Page, engine: Engine
) -> None:
    login(page, engine)
    page.get_by_role("link", name="Monitoring").click()
    expect(page.get_by_role("heading", name="Live monitoring")).to_be_visible()
    page.set_viewport_size({"width": 1280, "height": 420})
    trigger = _info(page, "Key attribution")
    trigger.evaluate("el => el.scrollIntoView({ block: 'center' })")
    trigger.click()
    pop = _popover(page)
    expect(pop).to_be_visible()
    # Neither side of the centered button can hold the content's natural height.
    sides = trigger.evaluate(
        "el => { const r = el.getBoundingClientRect();"
        " return [r.top - 8 - 16, innerHeight - 16 - r.bottom - 8]; }"
    )
    natural = pop.evaluate("el => el.scrollHeight")
    assert natural > max(sides), (natural, sides)
    _assert_contained(page, pop)
    assert _is_scrollable(pop)
    _end_is_reachable(pop)
    _shot(page, "05-short-viewport-scrolled-to-end", trigger, pop, engine)

    # Resizing and page scrolling keep it inside the viewport.
    page.set_viewport_size({"width": 900, "height": 360})
    _assert_contained(page, pop)
    page.mouse.move(5, 5)
    page.mouse.wheel(0, 120)
    page.wait_for_timeout(150)
    expect(pop).to_be_visible()
    _assert_contained(page, pop)
    page.set_viewport_size({"width": 1280, "height": 900})
    _assert_contained(page, pop)


def test_reduced_motion_disables_the_popover_animation(page: Page, engine: Engine) -> None:
    page.emulate_media(reduced_motion="reduce")
    login(page, engine)
    page.get_by_role("link", name="Logs").click()
    _info(page, "Logs").click()
    pop = _popover(page)
    expect(pop).to_be_visible()
    duration = pop.evaluate("el => parseFloat(getComputedStyle(el).animationDuration)")
    assert duration < 0.01


# --- Touch -------------------------------------------------------------------


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
    _assert_contained(phone, pop)
    box = pop.bounding_box()
    assert box
    _shot(phone, "06-touch-no-backend-call", trigger, pop, engine)
    # A tap on the popover itself keeps it open.
    pop.tap()
    expect(pop).to_be_visible()
    # A tap anywhere outside it closes it. (At phone width the popover may flip
    # above the button, so the outside point is taken from its real position.)
    y = box["y"] + box["height"] + 24
    if y > 844 - 8:
        y = box["y"] - 24
    phone.touchscreen.tap(8, y)
    expect(pop).to_be_hidden()


def test_phone_portrait_long_popover_contained(phone: Page, engine: Engine) -> None:
    login(phone, engine)
    phone.goto(f"{engine.dashboard}#/monitoring")
    trigger = _info(phone, "Refresh")
    trigger.tap()
    pop = _popover(phone)
    expect(pop).to_contain_text("GET /admin/alerts")
    expect(pop).to_contain_text("GET /admin/usage/attribution")
    expect(pop).to_contain_text("GET /admin/errors/taxonomy")
    _assert_contained(phone, pop)
    _end_is_reachable(pop)
    _shot(phone, "07-phone-portrait-three-entries", trigger, pop, engine)
    phone.keyboard.press("Escape")
    expect(pop).to_be_hidden()


def test_phone_landscape_long_popover_contained(phone_landscape: Page, engine: Engine) -> None:
    pg = phone_landscape
    login(pg, engine)
    pg.goto(f"{engine.dashboard}#/monitoring")
    trigger = _info(pg, "Key attribution")
    trigger.evaluate("el => el.scrollIntoView({ block: 'center' })")
    trigger.tap()
    pop = _popover(pg)
    expect(pop).to_contain_text("GET /admin/usage/attribution")
    _assert_contained(pg, pop)
    _end_is_reachable(pop)
    _shot(pg, "08-phone-landscape-key-attribution", trigger, pop, engine)


def test_touch_tap_wired_control_on_phone(phone: Page, engine: Engine) -> None:
    login(phone, engine)
    phone.goto(f"{engine.dashboard}#/logs")
    trigger = _info(phone, "Level filter")
    trigger.tap()
    pop = _popover(phone)
    expect(pop).to_contain_text("GET /logs")
    expect(pop).to_contain_text(_handler("GET", "/logs"))
    _assert_contained(phone, pop)
    _shot(phone, "09-touch-level-filter", trigger, pop, engine)
    phone.keyboard.press("Escape")
    expect(pop).to_be_hidden()
