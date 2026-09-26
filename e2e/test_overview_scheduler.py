"""The Overview's Scheduler and Energy cards against the real engine (A2c).

The engine's own ``GET /metrics`` snapshot is the truth source. The Scheduler
card must show the running scheduler's configuration (not "not running", no
invented zeros). The Energy card must report "measured" only when the engine
does, and otherwise give the engine's reason. Layout is checked at 1280 px and
390 px once live data has arrived.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from e2e.conftest import Engine, login, safe_screenshot


def _no_horizontal_scroll(page: Page, where: str) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"{where}: page scrolls horizontally by {overflow}px"


def test_scheduler_and_energy_cards_match_the_engine(page: Page, engine: Engine) -> None:
    with engine.api(engine.operator_key) as c:
        snap = c.get("/metrics").json()
    sched = snap["scheduler"]
    assert sched is not None, "the engine reports no scheduler"
    secrets = [engine.operator_key, engine.client_key]

    login(page, engine)
    card = page.get_by_test_id("scheduler-card")
    # Live data has arrived: the configured concurrency is shown, not a spinner.
    expect(card.get_by_text(f"/ {sched['max_concurrency']:,} slots")).to_be_visible()
    expect(card).not_to_contain_text("Scheduler not running")
    expect(card).not_to_contain_text("Waiting for metrics")
    for label in ("Admitted", "Rejected", "Cancelled", "Wait (avg / max / last)"):
        expect(card.get_by_text(label, exact=True)).to_be_visible()
    if sched["max_queue_depth"] > 0:
        expect(card.get_by_role("meter", name="Queue")).to_be_visible()
    else:
        expect(card.get_by_text("queueing disabled")).to_be_visible()

    # Energy can change state while the page streams (e.g. a baseline being
    # established), so compare the card with a fresh engine snapshot until they
    # agree, within a bound; a mismatch that persists fails.
    energy_card = page.locator(".card.stat").filter(has_text="Energy")
    expect(energy_card.locator(".sub")).to_be_visible()
    seen = ""
    for _ in range(10):
        with engine.api(engine.operator_key) as c:
            energy = c.get("/metrics").json()["energy"]
        value = energy_card.locator(".value").inner_text().strip()
        seen = energy_card.locator(".sub").inner_text().strip()
        if energy["state"] == "measured":
            ok = seen.startswith("source:") or seen == "measured"
        else:
            reason = energy.get("reason") or "no validated power probe"
            ok = value == "unavailable" and seen == f"Not measured — {reason}"
        if ok:
            break
        page.wait_for_timeout(1000)
    else:
        raise AssertionError(f"Energy card {seen!r} never matched the engine's {energy!r}")
    expect(page.get_by_test_id("metrics-stale")).to_have_count(0)

    _no_horizontal_scroll(page, "overview at 1280")
    safe_screenshot(page, "overview-scheduler-1280", secrets, full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(card.get_by_text("Admitted", exact=True)).to_be_visible()
    _no_horizontal_scroll(page, "overview at 390")
    safe_screenshot(page, "overview-scheduler-390", secrets, full_page=True)
