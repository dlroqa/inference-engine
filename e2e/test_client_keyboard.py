"""Keyboard selection of a client row in a real browser (A2c; required, no skip).

Uses the billing client the session fixture creates through the real admin API.
Tab reaches the client's row button; Enter opens its detail (and the address
changes) with focus kept on the button; Space closes it again, still without
test code moving focus. Pointer selection of the row still works.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from e2e.conftest import Engine, login, safe_screenshot


def test_client_row_is_keyboard_operable(page: Page, engine: Engine) -> None:
    with engine.api(engine.operator_key) as c:
        ids = [row["id"] for row in c.get("/admin/billing/clients").json()["clients"]]
    assert engine.client_id in ids, "the fixture client is not listed by the engine"

    login(page, engine)
    page.get_by_role("link", name="Clients").click()
    button = page.locator(f'button.rowbtn[data-client-id="{engine.client_id}"]')
    expect(button).to_be_visible()
    expect(button).to_have_attribute("aria-pressed", "false")
    detail = page.get_by_role("heading", name=f"Client {engine.client_id[:12]}")

    # Tab from the column's hint to the client's button (bounded).
    page.get_by_role("button", name=re.compile(r"^How this works: Select a client")).focus()
    for _ in range(80):
        if button.evaluate("el => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(button).to_be_focused()

    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(rf"#/clients/{re.escape(engine.client_id)}$"))
    expect(detail).to_be_visible()
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(button).to_be_focused()
    safe_screenshot(page, "client-keyboard-selected", [engine.operator_key, engine.client_key])

    page.keyboard.press("Space")
    expect(page).to_have_url(re.compile(r"#/clients$"))
    expect(detail).to_be_hidden()
    expect(button).to_have_attribute("aria-pressed", "false")
    expect(button).to_be_focused()

    # Pointer selection anywhere on the row still works.
    page.get_by_role("cell", name="e2e@example.com", exact=True).first.click()
    expect(page).to_have_url(re.compile(rf"#/clients/{re.escape(engine.client_id)}$"))
    expect(detail).to_be_visible()
