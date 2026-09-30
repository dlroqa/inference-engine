"""The Architecture view in a real browser against real engines (A3c).

Required, never skipped. Displayed values are compared with the engine's own
API responses read at the same time (auto-retrying expectations, no fixed
sleeps). Covers the nav entry, live fields, keyboard selection, destinations
with Back, deep links (canonical, alias, unknown), popover links from another
view, the narrow text presentation without horizontal scrolling, and a
restricted engine whose switches are off and whose model is loaded. No key is
printed; screenshots go through ``safe_screenshot``.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Locator, Page, expect

from e2e.conftest import Engine, RestrictedEngine, login, safe_screenshot


def _node(page: Page, label: str) -> Locator:
    """A node's entry in the text list (the presentation at every width)."""
    return page.locator("li.arch-list-item").filter(
        has=page.get_by_role("heading", name=label, exact=True, level=4)
    )


def _open(page: Page) -> None:
    page.get_by_role("link", name="Architecture", exact=True).click()
    expect(page.get_by_role("heading", name="Architecture", exact=True, level=1)).to_be_visible()


def _no_horizontal_scroll(page: Page, where: str) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"{where}: page scrolls horizontally by {overflow}px"


def _pool_text(backends: list[dict[str, Any]]) -> str:
    available = sum(1 for b in backends if b["available"])
    noun = "backend" if len(backends) == 1 else "backends"
    return f"{available} of {len(backends)} {noun} available"


def test_live_fields_match_the_engine(page: Page, engine: Engine) -> None:
    login(page, engine)
    _open(page)
    with engine.api(engine.operator_key) as c:
        system = c.get("/admin/system").json()
        backends = c.get("/admin/backends").json()["backends"]
        alerts = c.get("/admin/alerts").json()["alerts"]
        models = c.get("/admin/models").json()["models"]

    checks = system["readiness"]["checks"]
    expect(_node(page, "Store")).to_contain_text(
        f"SQLite · database {checks['database']} · migrations {checks['migrations']}"
    )
    expect(_node(page, "Backend pool")).to_contain_text(_pool_text(backends))
    loaded = sum(1 for m in models if m["loaded"])
    if models:
        noun = "model" if len(models) == 1 else "models"
        expect(_node(page, "Model registry")).to_contain_text(
            f"{len(models)} {noun} · {loaded} loaded"
        )
    else:
        expect(_node(page, "Model registry")).to_contain_text("No models registered")

    # The browser engine has webhooks on; the dead-letter state is the engine-wide alert.
    assert system["switches"]["webhooks_enabled"] is True
    dead = [a for a in alerts if a["kind"] == "webhook_dead_letters"]
    expect(_node(page, "Webhooks")).to_contain_text(
        dead[0]["message"] if dead else "Delivery on · none dead-lettered"
    )
    expect(_node(page, "Audit log")).to_contain_text("Chain not checked here")

    # Live metrics from the real stream (or its polling fallback).
    expect(_node(page, "Scheduler")).to_contain_text(re.compile(r"\d+/\d+ in use · \d+ queued"))
    expect(_node(page, "Telemetry")).to_contain_text(
        re.compile(r"Live stream connected|Polling /metrics")
    )
    expect(_node(page, "Scheduler")).to_contain_text(re.compile(r"/ws/metrics.*: observed \d"))

    page.set_viewport_size({"width": 1280, "height": 900})
    expect(page.get_by_role("group", name="Architecture diagram")).to_be_visible()
    _no_horizontal_scroll(page, "architecture 1280")
    safe_screenshot(page, "a3c-architecture-desktop", [engine.operator_key], full_page=True)


def test_keyboard_selection_destination_and_back(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.set_viewport_size({"width": 1280, "height": 900})
    _open(page)
    diagram = page.get_by_role("group", name="Architecture diagram")
    node = diagram.get_by_role("button", name=re.compile(r"^Backend pool"))
    node.focus()
    page.keyboard.press("Enter")
    expect(node).to_have_attribute("aria-pressed", "true")
    expect(page).to_have_url(re.compile(r"#/architecture\?focus=backend_pool$"))
    expect(node).to_be_focused()  # selecting does not move focus
    details = page.get_by_role("heading", name="Backend pool", exact=True, level=2)
    expect(details).to_be_visible()

    # The details open the view that holds the controls; Back returns to the node.
    card = page.locator(".card").filter(has=details)
    card.get_by_role("link", name=re.compile(r"^Open Backends & routing")).click()
    expect(page).to_have_url(re.compile(r"#/routing$"))
    expect(page.get_by_role("heading", name="Backends & routing", exact=True)).to_be_visible()
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/architecture\?focus=backend_pool$"))
    expect(page.get_by_role("heading", name="Backend pool", exact=True, level=2)).to_be_focused()
    page.go_forward()
    expect(page).to_have_url(re.compile(r"#/routing$"))


def test_deep_links_and_popover_links(page: Page, engine: Engine) -> None:
    login(page, engine)
    expect(page.get_by_role("region", name="Signed-in identity")).to_be_visible()

    page.goto(engine.dashboard + "#/architecture?focus=models")  # a documented alias
    expect(page.get_by_role("heading", name="Model registry", exact=True, level=2)).to_be_focused()

    page.goto(engine.dashboard + "#/architecture?focus=no-such-node")
    notice = page.get_by_role("status").filter(has_text="There is no no-such-node node")
    expect(notice).to_be_visible()
    expect(page.get_by_role("heading", name="Architecture", exact=True, level=1)).to_be_focused()

    # From another view: a "How this works" popover links each subsystem to its node.
    page.get_by_role("link", name="System", exact=True).click()
    expect(page.get_by_role("heading", name="System", exact=True, level=1)).to_be_visible()
    page.get_by_role("button", name="How this works: Refresh", exact=True).first.click()
    link = page.get_by_role("link", name="Open Gateway in Architecture", exact=True)
    expect(link.first).to_be_visible()
    link.first.click()
    expect(page).to_have_url(re.compile(r"#/architecture\?focus=gateway$"))
    expect(page.get_by_role("heading", name="Gateway", exact=True, level=2)).to_be_focused()


def test_narrow_screens_use_the_text_list(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(engine.dashboard + "#/architecture")
    expect(page.get_by_role("heading", name="Architecture", exact=True, level=1)).to_be_visible()
    expect(page.get_by_role("group", name="Architecture diagram")).to_be_hidden()
    expect(_node(page, "Gateway")).to_be_visible()
    _no_horizontal_scroll(page, "architecture 390")
    safe_screenshot(page, "a3c-architecture-phone", [engine.operator_key])


def test_restricted_engine_switches_and_loaded_model(
    page: Page, restricted: RestrictedEngine
) -> None:
    page.goto(restricted.dashboard)
    page.get_by_label("Operator API key").fill(restricted.operator_key)
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("region", name="Signed-in identity")).to_be_visible()
    _open(page)
    with restricted.api() as c:
        inference = c.get("/admin/system").json()["readiness"]["inference"]
    assert inference["available"] is True
    expect(_node(page, "Model service")).to_contain_text(
        "Model management off (allow_model_management=false)"
    )
    expect(_node(page, "Model service")).to_contain_text(
        "Network downloads: off (allow_network_downloads=false)"
    )
    model_id = inference.get("model_id") or "a model"
    expect(_node(page, "Backend")).to_contain_text(f"Serving {model_id}")
    safe_screenshot(page, "a3c-architecture-restricted", [restricted.operator_key], full_page=True)
