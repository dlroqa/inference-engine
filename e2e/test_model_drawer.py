"""The model details drawer in a real browser against the real engine (A2c).

The drawer is opened from the keyboard on this test's own imported copy of the
checksum-pinned fixture. The checksum it shows must equal both the workflow's
pin and the engine's ``GET /admin/models/{id}`` response. The imported file's
actual path must appear nowhere on the page. Also covered:

- reaching Details with the Tab key from the page content;
- Escape order (hint popover first, then the drawer);
- focus return, for an in-app open and for a direct link;
- Back and Forward;
- clipboard copy;
- layout at 1280 px and 390 px.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from e2e.conftest import Engine, import_ready_copy, login, safe_screenshot


def _no_horizontal_scroll(page: Page, where: str) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"{where}: page scrolls horizontally by {overflow}px"


def _settled(dialog: Locator) -> None:
    """Waits for the drawer's slide-in to finish, so layout is measured at rest."""
    dialog.evaluate("el => Promise.all(el.getAnimations().map((a) => a.finished))")


def _fits(dialog: Locator, width: int, where: str) -> None:
    _settled(dialog)
    box = dialog.bounding_box()
    assert box is not None, f"{where}: drawer has no box"
    assert box["x"] >= -0.5 and box["x"] + box["width"] <= width + 0.5, f"{where}: {box}"


def test_model_drawer_keyboard_checksum_history_and_layout(
    page: Page, engine: Engine, tmp_path: Path
) -> None:
    model = import_ready_copy(engine, tmp_path, "e2e-drawer")
    imported_path = str(tmp_path / f"{model['name']}.gguf")
    with engine.api(engine.operator_key) as c:
        detail = c.get(f"/admin/models/{model['id']}").json()
    # Server truth: the engine hashed the imported copy of the pinned fixture.
    assert detail["sha256"] == engine.model_sha256
    secrets = [engine.operator_key, engine.client_key]

    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    login(page, engine)
    page.get_by_role("link", name="Models").click()
    heading = page.get_by_role("heading", name="Models", level=1, exact=True)
    details = page.get_by_role("button", name=f"Details for {model['name']}", exact=True)
    dialog = page.get_by_role("dialog", name=f"Model details: {model['name']}")

    # Keyboard open from the list, reached by real Tab presses: navigating to
    # Models puts focus on the page content (the shell's focus policy), and Tab
    # moves through the page in order until this test's own row's Details
    # button has focus (bounded; nothing focuses it programmatically).
    expect(details).to_be_visible()
    expect(page.get_by_role("main")).to_be_focused()
    for _ in range(150):
        if details.evaluate("el => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    else:
        raise AssertionError(f"Details for {model['name']} not reached within 150 Tab presses")
    expect(details).to_be_focused()
    page.keyboard.press("Enter")
    expect(dialog).to_be_visible()
    expect(page).to_have_url(re.compile(rf"#/models/{re.escape(model['id'])}$"))
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_focused()

    sha = dialog.get_by_test_id("model-sha256")
    expect(sha).to_have_text(engine.model_sha256)
    assert sha.inner_text().strip() == detail["sha256"]
    expect(dialog.get_by_test_id("model-source")).to_have_text("Local import (path not shown)")
    assert imported_path not in page.content(), "the imported file's path is on the page"
    assert str(tmp_path) not in page.content(), "the import directory is on the page"

    # Copying the checksum reaches the clipboard and says so.
    dialog.get_by_role("button", name="Copy checksum", exact=True).click()
    expect(dialog.get_by_text("Checksum copied to the clipboard.")).to_be_visible()
    assert page.evaluate("() => navigator.clipboard.readText()") == engine.model_sha256

    _fits(dialog, 1280, "drawer at 1280")
    _no_horizontal_scroll(page, "drawer at 1280")
    safe_screenshot(page, "model-drawer-1280", secrets)

    # Escape closes an open hint popover first, then the drawer.
    dialog.get_by_role("button", name="How this works: Model details", exact=True).click()
    pop = dialog.get_by_role("group", name="How this works: Model details")
    expect(pop).to_contain_text("GET /admin/models/{model_id}")
    page.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#/models$"))
    expect(details).to_be_focused()

    # Back closes and Forward reopens; closing then returns to the list entry.
    details.click()
    expect(dialog).to_be_visible()
    page.go_back()
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#/models$"))
    page.go_forward()
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#/models$"))
    expect(details).to_be_focused()

    # Direct link: the app starts on the drawer; closing replaces it with the
    # list and focuses the heading (even though a Details button exists).
    page.goto(f"{engine.dashboard}#/models/{model['id']}")
    page.reload()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("model-sha256")).to_have_text(engine.model_sha256)
    expect(details).to_be_attached()
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#/models$"))
    expect(heading).to_be_focused()

    # Narrow layout: the drawer takes the full width without page scroll.
    page.set_viewport_size({"width": 390, "height": 844})
    details.click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("model-details")).to_be_visible()
    _fits(dialog, 390, "drawer at 390")
    _no_horizontal_scroll(page, "drawer at 390")
    safe_screenshot(page, "model-drawer-390", secrets)
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).to_be_hidden()

    # Nothing about the model changed.
    with engine.api(engine.operator_key) as c:
        after = c.get(f"/admin/models/{model['id']}").json()
    assert after["status"] == "ready"
    assert after["sha256"] == engine.model_sha256
