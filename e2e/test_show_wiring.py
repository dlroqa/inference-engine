"""The "Show wiring" toggle in a real browser.

Off by default; when on, every wired "How this works" hint gains a METHOD /path
chip taken from the wiring registry. The choice survives a reload (browser
storage) and the layout stays within the viewport at desktop and phone widths.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from e2e.conftest import Engine, login, safe_screenshot

VIEWS = ["overview", "monitoring", "logs", "models", "clients", "keys", "security"]


def _no_horizontal_scroll(page: Page) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"page scrolls horizontally by {overflow}px"


def test_show_wiring_chips_persist_and_fit(page: Page, engine: Engine) -> None:
    secrets = [engine.operator_key, engine.client_key]
    login(page, engine)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    toggle = page.get_by_role("button", name="Show wiring", exact=True)
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_test_id("wiring-chips")).to_have_count(0)

    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".wired-chip", has_text="GET /admin/overview").first).to_be_visible()
    expect(page.locator(".wired-chip", has_text="WS /ws/metrics").first).to_be_visible()

    # Persisted in this browser.
    page.reload()
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    expect(toggle).to_have_attribute("aria-pressed", "true")

    page.goto(f"{engine.dashboard}#/models")
    expect(page.locator(".wired-chip", has_text="GET /admin/models").first).to_be_visible()
    expect(page.locator(".wired-chip", has_text="GET /admin/system").first).to_be_visible()
    for view in VIEWS:
        page.goto(f"{engine.dashboard}#/{view}")
        expect(page.locator("main h1").first).to_be_visible()
        _no_horizontal_scroll(page)
    page.goto(f"{engine.dashboard}#/overview")
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    safe_screenshot(page, "show-wiring-desktop", secrets)

    # Phone width: chips wrap instead of widening the page.
    page.set_viewport_size({"width": 390, "height": 844})
    for view in VIEWS:
        page.goto(f"{engine.dashboard}#/{view}")
        expect(page.locator("main h1").first).to_be_visible()
        _no_horizontal_scroll(page)
    page.goto(f"{engine.dashboard}#/models")
    expect(page.get_by_role("heading", name="Models", exact=True)).to_be_visible()
    safe_screenshot(page, "show-wiring-phone", secrets)

    # Dialogs are unaffected: the change-key dialog opens and Escape closes it.
    page.get_by_role("button", name="Change key").click()
    dialog = page.get_by_role("dialog", name="Change operator key")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)

    # Turning it off hides every chip, and that choice persists too.
    page.set_viewport_size({"width": 1280, "height": 900})
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_test_id("wiring-chips")).to_have_count(0)
    page.reload()
    expect(page.get_by_role("heading", name="Models", exact=True)).to_be_visible()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_test_id("wiring-chips")).to_have_count(0)
