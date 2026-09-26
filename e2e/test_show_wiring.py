"""The "Show wiring" toggle in a real browser, measured on populated pages.

Off by default; when on, every wired "How this works" hint gains a METHOD /path
chip taken from the wiring registry. The choice survives a reload (browser
storage).

Layout is measured only after each view shows content this test created itself
(never data left behind by another test), so a heading that renders before its
rows cannot make the check pass early:

- overview: a live metrics snapshot (the Uptime card has a value);
- monitoring: the error taxonomy counts this test's failed request;
- logs: the row for this test's request id;
- models: this test's own ready model row with its endpoint chips;
- clients: the billing client the session fixture creates;
- keys: this test's key row;
- security: the audit record of creating that key.

With chips on, the document must not scroll horizontally at 1280 px or 390 px.
Wide tables still scroll inside their own card. A real delete confirmation with
its endpoint chip is then checked for fit, focus containment and Escape order,
and cancelled without deleting anything. This is bounded acceptance, not an
exhaustive responsive or assistive-technology audit.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, expect

from e2e.conftest import (
    Engine,
    create_operator_key,
    import_ready_copy,
    login,
    purge_key,
    safe_screenshot,
)


def _no_horizontal_scroll(page: Page, where: str) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"{where}: page scrolls horizontally by {overflow}px"


def _failed_request_id(engine: Engine) -> str:
    """A request that fails at the edge, so it is logged and categorized."""
    with engine.api(engine.operator_key) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={"model": "no-such-model-e2e", "messages": [{"role": "user", "content": "x"}]},
        )
        assert resp.status_code >= 400, resp.status_code
        request_id = resp.headers["x-request-id"]
        deadline = time.monotonic() + 15
        while not c.get("/logs", params={"request_id": request_id}).json()["events"]:
            assert time.monotonic() < deadline, "the failed request was not logged"
            time.sleep(0.25)
    return request_id


def _markers(
    page: Page, request_id: str, model: dict[str, str], key_label: str
) -> dict[str, Callable[[], None]]:
    def overview() -> None:
        uptime = page.locator(".card.stat").filter(has_text="Uptime").locator(".value")
        expect(uptime).not_to_have_text("—")
        expect(page.locator(".wired-chip", has_text="WS /ws/metrics").first).to_be_visible()

    def monitoring() -> None:
        expect(page.get_by_text(re.compile(r"^[1-9]\d* categorized errors$"))).to_be_visible()
        expect(page.locator(".wired-chip", has_text="/admin/errors/taxonomy").first).to_be_visible()

    def logs() -> None:
        expect(page.get_by_role("cell", name=request_id[:16], exact=True).first).to_be_visible()
        expect(page.locator(".wired-chip", has_text="GET /logs").first).to_be_visible()

    def models() -> None:
        row = page.locator("div.card").filter(
            has=page.locator("span.mono", has_text=re.compile(rf"^{re.escape(model['name'])}$"))
        )
        expect(row.get_by_role("button", name="Load", exact=True)).to_be_visible()
        expect(row.locator(".wired-chip", has_text="/load")).to_be_visible()

    def clients() -> None:
        expect(page.get_by_role("cell", name="e2e@example.com", exact=True).first).to_be_visible()
        chip = page.locator(".wired-chip", has_text="GET /admin/billing/clients")
        expect(chip.first).to_be_visible()

    def keys() -> None:
        expect(page.get_by_text(key_label, exact=True).first).to_be_visible()
        expect(page.locator(".wired-chip", has_text="GET /admin/keys").first).to_be_visible()

    def security() -> None:
        expect(page.get_by_role("cell", name="key.create").first).to_be_visible()
        expect(page.locator(".wired-chip", has_text="GET /admin/audit").first).to_be_visible()

    return {
        "overview": overview,
        "monitoring": monitoring,
        f"logs?request_id={request_id}": logs,
        "models": models,
        "clients": clients,
        "keys": keys,
        "security": security,
    }


def test_show_wiring_chips_persist_and_fit_populated_views(
    page: Page, engine: Engine, tmp_path: Path
) -> None:
    model = import_ready_copy(engine, tmp_path, "wiring-ready")
    key = create_operator_key(engine, "wiring-layout")
    secrets = [engine.operator_key, engine.client_key, key.token]
    try:
        request_id = _failed_request_id(engine)
        markers = _markers(page, request_id, model, key.label)

        login(page, engine)
        expect(page.get_by_role("heading", name="Overview")).to_be_visible()
        toggle = page.get_by_role("button", name="Show wiring", exact=True)
        expect(toggle).to_have_attribute("aria-pressed", "false")
        expect(page.get_by_test_id("wiring-chips")).to_have_count(0)

        toggle.click()
        expect(toggle).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".wired-chip", has_text="GET /admin/overview").first).to_be_visible()

        # Persisted in this browser.
        page.reload()
        expect(page.get_by_role("heading", name="Overview")).to_be_visible()
        expect(toggle).to_have_attribute("aria-pressed", "true")

        for width, height, label in ((1280, 900, "desktop"), (390, 844, "phone")):
            page.set_viewport_size({"width": width, "height": height})
            for view, loaded in markers.items():
                page.goto(f"{engine.dashboard}#/{view}")
                loaded()
                _no_horizontal_scroll(page, f"{view} at {width}px")
            page.goto(f"{engine.dashboard}#/models")
            markers["models"]()
            safe_screenshot(page, f"show-wiring-models-{label}", secrets, full_page=True)

        _delete_dialog_with_chips(page, engine, model, secrets)

        # Turning it off hides every chip, and that choice persists too.
        page.set_viewport_size({"width": 1280, "height": 900})
        toggle.click()
        expect(toggle).to_have_attribute("aria-pressed", "false")
        expect(page.get_by_test_id("wiring-chips")).to_have_count(0)
        page.reload()
        expect(page.get_by_role("heading", name="Models", exact=True)).to_be_visible()
        expect(toggle).to_have_attribute("aria-pressed", "false")
        expect(page.get_by_test_id("wiring-chips")).to_have_count(0)
    finally:
        purge_key(engine, key.id)
        with engine.api(engine.operator_key) as c:
            c.delete(f"/admin/models/{model['id']}")


def _delete_dialog_with_chips(
    page: Page, engine: Engine, model: dict[str, str], secrets: list[str]
) -> None:
    """A real destructive confirmation, with chips on, cancelled without a write."""
    for width, height, label in ((1280, 900, "desktop"), (390, 844, "phone")):
        page.set_viewport_size({"width": width, "height": height})
        page.goto(f"{engine.dashboard}#/models")
        opener = page.get_by_role("button", name=f"Delete {model['name']}", exact=True)
        expect(opener).to_be_enabled()
        opener.click()
        dialog = page.get_by_role("dialog", name=f"Delete {model['name']}?")
        expect(dialog).to_be_visible()
        chip = dialog.locator(".wired-chip", has_text="DELETE /admin/models/{model_id}")
        expect(chip).to_be_visible()

        box = dialog.bounding_box()
        assert box is not None
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, (label, box)
        assert box["y"] >= 0 and box["y"] + box["height"] <= height + 0.5, (label, box)
        _no_horizontal_scroll(page, f"delete dialog at {width}px")
        expect(dialog.get_by_role("button", name="Delete model", exact=True)).to_be_enabled()
        cancel = dialog.get_by_role("button", name="Cancel", exact=True)
        expect(cancel).to_be_focused()

        # Tab stays inside the dialog.
        for _ in range(6):
            page.keyboard.press("Tab")
            assert dialog.evaluate("el => el.contains(document.activeElement)"), label

        # First Escape closes an open popover, the next closes the dialog.
        hint = dialog.get_by_role("button", name="How this works: Delete model", exact=True)
        hint.focus()
        pop = page.get_by_role("group", name="How this works: Delete model")
        expect(pop).to_be_visible()
        safe_screenshot(page, f"show-wiring-delete-dialog-{label}", secrets)
        page.keyboard.press("Escape")
        expect(pop).to_have_count(0)
        expect(dialog).to_be_visible()
        expect(hint).to_be_focused()
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        expect(opener).to_be_focused()

    # Cancelled: nothing was deleted.
    with engine.api(engine.operator_key) as c:
        resp = c.get(f"/admin/models/{model['id']}")
        assert resp.status_code == 200, resp.status_code
        assert resp.json()["status"] == "ready"
