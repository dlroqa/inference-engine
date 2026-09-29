"""System and Backends & Routing views (A3a) in a real browser.

Claims are kept separate, as elsewhere in this suite:

- Rendered state matches the engine: the System view shows what
  ``GET /admin/system`` reports (build, readiness checks, every switch), and the
  pool table shows every backend ``GET /admin/backends`` reports.
- The route-plan dry run sends exactly ``{model, required_features}`` (never a
  prompt) and shows the choice the engine returns for that same body.
- Diagnostics: the main engine serves the bundle and the browser saves it; the
  restricted engine (``diagnostics_enabled=false``) refuses it, and the view
  disables the control with the switch named. The bundle is parsed only for its
  shape and secret absence; it is never printed, screenshotted or kept.
- Authorization stays distinct from the switch: a client key gets
  ``operator_role_required``, not ``diagnostics_disabled``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from e2e.conftest import Engine, RestrictedEngine, assert_no_secrets, login, safe_screenshot

SWITCH_ROWS = (
    "allow_model_management",
    "allow_network_downloads",
    "allow_structured_output",
    "diagnostics_enabled",
    "require_auth",
    "webhooks_enabled",
    "client_events_enabled",
    "ip_allowlist_set",
    "grpc_enabled",
)


def _login_restricted(page: Page, engine: RestrictedEngine) -> None:
    page.goto(engine.dashboard)
    page.get_by_label("Operator API key").fill(engine.operator_key)
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("region", name="Signed-in identity")).to_be_visible()


def _system(engine: Engine) -> dict[str, object]:
    with engine.api(engine.operator_key) as c:
        resp = c.get("/admin/system")
        assert resp.status_code == 200, resp.status_code
        return resp.json()


def _switch_row(page: Page, name: str) -> Locator:
    table = page.get_by_role("region", name="Feature switches table")
    return table.locator("tbody tr").filter(has=page.get_by_text(name, exact=True))


# --- System -------------------------------------------------------------------


def test_system_view_shows_what_the_engine_reports(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="System", exact=True).click()
    expect(page.get_by_role("heading", name="System", exact=True)).to_be_visible()
    expect(page).to_have_url(f"{engine.dashboard}#/system")
    system = _system(engine)

    build = system["build"]
    assert isinstance(build, dict)
    expect(page.get_by_text(build["version"], exact=True)).to_be_visible()
    commit = build["commit"] or "not recorded"
    expect(page.locator("dl").get_by_text(commit).first).to_be_visible()

    readiness = system["readiness"]
    assert isinstance(readiness, dict)
    overall = "Ready" if readiness["ready"] else "Not ready"
    expect(page.get_by_text(overall, exact=True)).to_be_visible()
    for name, value in readiness["checks"].items():
        row = page.locator("tr").filter(has=page.get_by_role("cell", name=name, exact=True))
        expect(row).to_contain_text(value)

    switches = system["switches"]
    assert isinstance(switches, dict) and set(switches) == set(SWITCH_ROWS)
    for name in SWITCH_ROWS:
        row = _switch_row(page, name)
        expect(row).to_contain_text("On" if switches[name] else "Off")
    # Read-only: the switch table has no controls.
    table = page.get_by_role("region", name="Feature switches table")
    assert table.locator("button, input, select, textarea").count() == 0
    safe_screenshot(page, "a3a-system", [engine.operator_key, engine.client_key], full_page=True)


def test_diagnostics_bundle_is_saved_not_shown(page: Page, engine: Engine, tmp_path: Path) -> None:
    login(page, engine)
    page.goto(f"{engine.dashboard}#/system")
    button = page.get_by_role("button", name="Download diagnostics bundle", exact=True)
    expect(button).to_be_enabled()
    with page.expect_download() as info:
        button.click()
    download = info.value
    assert download.suggested_filename.startswith("inference-engine-diagnostics-")
    assert download.suggested_filename.endswith(".json")
    path = tmp_path / "bundle.json"
    download.save_as(path)
    try:
        text = path.read_text(encoding="utf-8")
        bundle = json.loads(text)
        assert isinstance(bundle, dict)
        assert {"engine_version", "config"} <= set(bundle)
        # The redactor keeps credentials out of the bundle.
        assert engine.operator_key not in text, "operator key in the diagnostics bundle"
        assert engine.client_key not in text, "client key in the diagnostics bundle"
    finally:
        path.unlink(missing_ok=True)
        download.delete()
    expect(page.get_by_role("status").filter(has_text="bundle to your downloads")).to_be_visible()
    # Nothing from the bundle is rendered.
    assert "recent_logs" not in page.content()
    assert_no_secrets(page, [engine.operator_key, engine.client_key])


def test_diagnostics_disabled_is_explained_and_refused(
    page: Page, restricted: RestrictedEngine
) -> None:
    with restricted.api() as c:
        switches = c.get("/admin/system").json()["switches"]
        refused = c.get("/diagnostics")
    # Server truth first: this engine runs with the bundle off.
    assert switches["diagnostics_enabled"] is False, "restricted engine has diagnostics on"
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "diagnostics_disabled"

    _login_restricted(page, restricted)
    page.goto(f"{restricted.dashboard}#/system")
    button = page.get_by_role("button", name="Download diagnostics bundle", exact=True)
    expect(button).to_be_disabled()
    described = button.get_attribute("aria-describedby")
    assert described
    expect(page.locator(f"#{described}")).to_contain_text("diagnostics_enabled=false")
    expect(_switch_row(page, "diagnostics_enabled")).to_contain_text("Off")
    safe_screenshot(page, "a3a-system-diagnostics-off", [restricted.operator_key])


def test_client_key_is_an_authorization_failure_not_a_disabled_switch(engine: Engine) -> None:
    with engine.api(engine.client_key) as c:
        resp = c.get("/diagnostics")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "operator_role_required"


# --- Backends & Routing ------------------------------------------------------------


def test_routing_view_lists_the_pool_the_engine_reports(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="Backends & routing", exact=True).click()
    expect(page.get_by_role("heading", name="Backends & routing")).to_be_visible()
    with engine.api(engine.operator_key) as c:
        pool = c.get("/admin/backends").json()
    table = page.get_by_role("region", name="Backend pool table")
    rows = table.locator("tbody tr")
    expect(rows).to_have_count(pool["count"])
    for b in pool["backends"]:
        row = rows.filter(has=page.locator("span.mono", has_text=b["name"]))
        expect(row).to_contain_text(b["state"])
        expect(row).to_contain_text(f"{b['in_flight']} / {b['max_in_flight']}")
        expect(row).to_contain_text("Yes" if b["available"] else "No")
    expect(page.get_by_text("Averages", exact=False).first).to_be_visible()
    assert "p95" not in page.content() and "p50" not in page.content()
    safe_screenshot(page, "a3a-routing", [engine.operator_key, engine.client_key], full_page=True)


def test_route_plan_sends_no_prompt_and_matches_the_engine(
    page: Page, restricted: RestrictedEngine
) -> None:
    _login_restricted(page, restricted)
    page.goto(f"{restricted.dashboard}#/routing")
    model = page.get_by_label("Model name")
    expect(model).to_be_visible()

    # Keyboard only: type, Tab to the checkbox, Space, Tab to submit, Enter.
    model.focus()
    page.keyboard.type("tiny")
    page.keyboard.press("Tab")
    checkbox = page.get_by_label("Requires structured output (JSON / grammar)")
    expect(checkbox).to_be_focused()
    page.keyboard.press("Space")
    expect(checkbox).to_be_checked()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Plan route", exact=True)).to_be_focused()
    with page.expect_request(
        lambda r: r.url.endswith("/admin/route/plan") and r.method == "POST"
    ) as req:
        page.keyboard.press("Enter")
    body = req.value.post_data_json
    assert body == {"model": "tiny", "required_features": ["structured_output"]}, body

    with restricted.api() as c:
        expected = c.post("/admin/route/plan", json=body).json()
    result = page.get_by_test_id("route-plan-result")
    if expected["chosen"] is not None:
        expect(result).to_contain_text(f"Would choose {expected['chosen']}")
    else:
        expect(result).to_contain_text("No backend could take this request right now")
    expect(result).to_contain_text(expected["policy"])
    expect(result).to_contain_text("structured_output")
    safe_screenshot(page, "a3a-route-plan", [restricted.operator_key])

    # The plain request (no features) for the loaded model chooses a backend.
    checkbox.uncheck()
    with page.expect_request(lambda r: r.url.endswith("/admin/route/plan")) as req2:
        page.get_by_role("button", name="Plan route", exact=True).click()
    assert req2.value.post_data_json == {"model": "tiny", "required_features": []}
    with restricted.api() as c:
        plain = c.post("/admin/route/plan", json={"model": "tiny", "required_features": []}).json()
    assert plain["chosen"] is not None, "the loaded model has no available backend"
    expect(result).to_contain_text(f"Would choose {plain['chosen']}")


def test_deep_links_and_history_between_the_new_views(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.goto(f"{engine.dashboard}#/system")
    expect(page.get_by_role("heading", name="System", exact=True)).to_be_visible()
    page.get_by_role("link", name="Backends & routing", exact=True).click()
    expect(page.get_by_role("heading", name="Backends & routing")).to_be_visible()
    expect(page.locator("main")).to_be_focused()
    page.go_back()
    expect(page.get_by_role("heading", name="System", exact=True)).to_be_visible()
    system_link = page.get_by_role("link", name="System", exact=True)
    expect(system_link).to_have_attribute("aria-current", "page")


@pytest.fixture
def phone(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    pg = context.new_page()
    pg.set_default_timeout(20_000)
    try:
        yield pg
    finally:
        context.close()


@pytest.mark.parametrize("view", ["system", "routing"])
def test_narrow_layout_has_no_horizontal_page_scroll(
    phone: Page, engine: Engine, view: str
) -> None:
    login(phone, engine)
    phone.goto(f"{engine.dashboard}#/{view}")
    heading = "System" if view == "system" else "Backends & routing"
    expect(phone.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    table = "Feature switches table" if view == "system" else "Backend pool table"
    expect(phone.get_by_role("region", name=table)).to_be_visible()
    overflow = phone.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"page scrolls horizontally by {overflow}px at 390px"
    # Wide tables scroll inside their card instead.
    region = phone.get_by_role("region", name=table)
    assert region.evaluate("el => getComputedStyle(el).overflowX") in ("auto", "scroll")
    safe_screenshot(phone, f"a3a-{view}-390", [engine.operator_key, engine.client_key])
