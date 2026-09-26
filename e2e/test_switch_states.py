"""Feature-switch disabled states, in a real browser against a restricted engine.

The restricted engine runs with ``allow_model_management`` and
``allow_network_downloads`` off. Its data directory was seeded beforehand
(``e2e/seed_restricted.py``) and the fixture is configured as its loaded model,
so the Models view has a loaded row (Unload) and a ready row (Load, Delete).

Two claims are checked separately:

- The engine refuses every switch-gated model operation with the feature code
  ``model_management_disabled`` and nothing changes (server truth).
- The dashboard shows those controls disabled, not hidden, with a visible reason
  that each disabled control references (``aria-describedby``).

Cancelling a download is not switch-gated; with downloads off there is no
download to cancel here, so that case is covered by the component tests only.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, expect

from e2e.conftest import RestrictedEngine, safe_screenshot

READY = "tiny-ready"
LOADED = "tiny"


def _login(page: Page, engine: RestrictedEngine) -> None:
    page.goto(engine.dashboard)
    page.get_by_label("Operator API key").fill(engine.operator_key)
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("region", name="Signed-in identity")).to_be_visible()


def _models(engine: RestrictedEngine) -> dict[str, dict[str, object]]:
    with engine.api() as c:
        resp = c.get("/admin/models")
        assert resp.status_code == 200, resp.status_code
        return {m["name"]: m for m in resp.json()["models"]}


def _row(page: Page, name: str) -> Locator:
    exact = page.locator("span.mono", has_text=re.compile(rf"^{re.escape(name)}$"))
    return page.locator("div.card").filter(has=exact)


def _described_by(control: Locator) -> str:
    ids = (control.get_attribute("aria-describedby") or "").split()
    assert ids, "disabled control has no aria-describedby"
    page = control.page
    return " ".join(page.locator(f"#{i}").inner_text() for i in ids)


def test_engine_refuses_switch_gated_model_operations(restricted: RestrictedEngine) -> None:
    before = _models(restricted)
    assert before[READY]["status"] == "ready" and before[READY]["loaded"] is False
    assert before[LOADED]["loaded"] is True
    ready_id = before[READY]["id"]
    loaded_id = before[LOADED]["id"]
    cases: list[tuple[str, str, dict[str, object] | None]] = [
        ("POST", "/admin/models/import", {"path": "/nonexistent/model.gguf"}),
        (
            "POST",
            "/admin/models/download",
            {"source_type": "url", "url": "https://example.invalid/model.gguf"},
        ),
        ("POST", f"/admin/models/{ready_id}/load", None),
        ("POST", f"/admin/models/{loaded_id}/unload", None),
        ("DELETE", f"/admin/models/{ready_id}", None),
    ]
    with restricted.api() as c:
        for method, path, body in cases:
            resp = c.request(method, path, json=body)
            assert resp.status_code == 403, (method, path, resp.status_code)
            assert resp.json()["error"]["code"] == "model_management_disabled", (method, path)
    after = _models(restricted)
    assert set(after) == set(before)
    assert after[LOADED]["loaded"] is True and after[READY]["loaded"] is False


def test_dashboard_disables_gated_controls_with_reasons(
    page: Page, restricted: RestrictedEngine
) -> None:
    _login(page, restricted)
    page.goto(f"{restricted.dashboard}#/models")
    expect(page.get_by_role("heading", name="Models", exact=True)).to_be_visible()

    notice = page.locator("#models-switch-notice")
    expect(notice).to_contain_text("Model management is disabled (allow_model_management=false).")
    expect(notice).to_contain_text("Loading, unloading and deleting models is unavailable")

    ready, loaded = _row(page, READY), _row(page, LOADED)
    expect(ready).to_have_count(1)
    expect(loaded).to_have_count(1)
    gated = [
        ready.get_by_role("button", name="Load", exact=True),
        ready.get_by_role("button", name=f"Delete {READY}", exact=True),
        loaded.get_by_role("button", name="Unload", exact=True),
    ]
    for control in gated:
        # Shown and explained, not hidden.
        expect(control).to_be_visible()
        expect(control).to_be_disabled()
        assert "allow_model_management=false" in _described_by(control)

    # The add form explains every switch its selected mode needs.
    download = page.get_by_role("button", name="Download model", exact=True)
    expect(download).to_be_disabled()
    reason = _described_by(download)
    assert "allow_model_management=false, allow_network_downloads=false" in reason
    page.get_by_role("tab", name="Local file", exact=True).click()
    imp = page.get_by_role("button", name="Import model", exact=True)
    expect(imp).to_be_disabled()
    reason = _described_by(imp)
    assert "allow_model_management=false" in reason
    assert "allow_network_downloads" not in reason

    # The row's "How this works" names the switch and its live state.
    page.get_by_role("button", name=f"How this works: Actions for {READY}", exact=True).click()
    pop = page.get_by_role("group", name=f"How this works: Actions for {READY}")
    expect(pop).to_contain_text("allow_model_management — currently off")
    page.keyboard.press("Escape")

    safe_screenshot(page, "switches-off-models", [restricted.operator_key], full_page=True)

    # The UI sent nothing that changed the engine.
    after = _models(restricted)
    assert after[LOADED]["loaded"] is True and after[READY]["status"] == "ready"
