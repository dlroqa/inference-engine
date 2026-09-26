"""Operator dashboard, end to end in a real browser against a running engine.

Every write is checked on the backend too, so the UI is proven to be wired to
the real function it claims, not just to render.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from e2e.conftest import Engine, login


def _chat(max_tokens: int) -> dict[str, object]:
    return {
        "model": "tiny",
        "messages": [{"role": "user", "content": "Say hi"}],
        "max_tokens": max_tokens,
    }


def test_no_key_shows_the_key_prompt(page: Page, engine: Engine) -> None:
    page.goto(engine.dashboard)
    expect(page.get_by_role("heading", name="Operator access", exact=True)).to_be_visible()
    expect(page.get_by_label("Operator API key")).to_be_visible()


def test_invalid_key_is_rejected(page: Page, engine: Engine) -> None:
    login(page, engine, key="sk-ie-definitely-not-valid")
    expect(page.get_by_role("alert")).to_contain_text("not accepted")


def test_client_key_is_denied_in_ui_and_backend(page: Page, engine: Engine) -> None:
    with engine.api(engine.client_key) as c:
        resp = c.get("/admin/overview")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "operator_role_required"
    login(page, engine, key=engine.client_key)
    expect(page.get_by_role("heading", name="Operator access required")).to_be_visible()
    # The client key keeps working for inference: the denial is role-based.
    page.get_by_role("button", name="Forget saved key").click()
    expect(page.get_by_role("heading", name="Operator access", exact=True)).to_be_visible()


def test_operator_login_shows_identity_without_the_token(page: Page, engine: Engine) -> None:
    login(page, engine)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    identity = page.get_by_role("region", name="Signed-in identity")
    expect(identity).to_contain_text("owner")
    expect(identity).to_contain_text("operator")
    assert engine.operator_key not in page.content()


def test_deep_links_and_browser_history(page: Page, engine: Engine) -> None:
    login(page, engine)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.goto(f"{engine.dashboard}#/logs?request_id=chatcmpl-e2e-probe")
    expect(page.get_by_role("heading", name="Logs")).to_be_visible()
    expect(page.get_by_text("Showing events for request")).to_be_visible()
    page.get_by_role("link", name="API keys").click()
    expect(page).to_have_url(re.compile(r"#/keys$"))
    expect(page.get_by_role("heading", name="API keys")).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="Logs")).to_be_visible()
    page.go_forward()
    expect(page.get_by_role("heading", name="API keys")).to_be_visible()
    page.goto(f"{engine.dashboard}#/does-not-exist")
    expect(page.get_by_role("heading", name="Page not available")).to_be_visible()


def test_malformed_hash_recovers_without_reloading(page: Page, engine: Engine) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    login(page, engine)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    # Fresh document with a malformed initial hash and an already saved key.
    page.goto(f"{engine.dashboard}#/%")
    page.reload()
    expect(page.get_by_role("heading", name="Page not available")).to_be_visible()
    page.get_by_role("button", name="Go to Overview").click()
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    # An in-document hash change must not throw or leave the previous view stale.
    page.evaluate("window.location.hash = '#/clients/%FF'")
    expect(page.get_by_role("heading", name="Page not available")).to_be_visible()
    page.get_by_role("link", name="Models").click()
    expect(page.get_by_role("heading", name="Models")).to_be_visible()
    assert not errors, "browser reported an uncaught error during route recovery"


def test_model_import_load_and_generate(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="Models").click()
    page.get_by_role("tab", name="Local file").click()
    page.get_by_label("Local file path").fill(engine.model_path)
    page.get_by_label("Name (optional)").fill("tiny")
    page.get_by_role("button", name="Import model").click()
    expect(page.get_by_text("tiny", exact=True)).to_be_visible(timeout=60_000)

    page.get_by_role("button", name=re.compile(r"^Load")).click()
    expect(page.get_by_text("loaded", exact=True)).to_be_visible(timeout=120_000)

    # Backend outcome: the registry reports it loaded, readiness says inference
    # is available, and the real model generates through the OpenAI edge.
    with engine.api(engine.operator_key) as c:
        models = c.get("/admin/models").json()["models"]
        assert any(m["name"] == "tiny" and m["loaded"] for m in models)
        ready = c.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["inference"]["available"] is True
        gen = c.post("/v1/chat/completions", json=_chat(max_tokens=8))
        assert gen.status_code == 200
        assert gen.json()["choices"][0]["message"]["content"] is not None
    # Client keys still serve inference (role split, not a lockout).
    with engine.api(engine.client_key) as c:
        gen = c.post("/v1/chat/completions", json=_chat(max_tokens=4))
        assert gen.status_code == 200

    page.get_by_role("link", name="Overview").click()
    expect(page.get_by_test_id("engine-readiness")).to_contain_text("ready")


def test_keyboard_confirmation_and_cancellation(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("link", name="API keys").click()
    page.get_by_label("Label (optional)").fill("e2e-temp")
    page.get_by_role("button", name="Create key").click()
    expect(page.get_by_test_id("new-token")).to_be_visible()

    with engine.api(engine.operator_key) as c:
        key = next(k for k in c.get("/admin/keys").json()["keys"] if k["label"] == "e2e-temp")
    revoke = page.get_by_role("button", name=f"Revoke key {key['prefix']}")

    # Escape cancels: nothing changes on the backend.
    revoke.click()
    dialog = page.get_by_role("dialog", name="Revoke this key?")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(revoke).to_be_focused()
    with engine.api(engine.operator_key) as c:
        still = next(k for k in c.get("/admin/keys").json()["keys"] if k["id"] == key["id"])
        assert still["revoked"] is False

    # Keyboard-only confirm: Tab from Cancel to the confirm button, press Enter.
    revoke.click()
    expect(dialog).to_be_visible()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="Revoke key")).to_be_focused()
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    with engine.api(engine.operator_key) as c:
        gone = next(k for k in c.get("/admin/keys").json()["keys"] if k["id"] == key["id"])
        assert gone["revoked"] is True


def test_change_and_forget_key(page: Page, engine: Engine) -> None:
    login(page, engine)
    page.get_by_role("button", name="Change key").click()
    dialog = page.get_by_role("dialog", name="Change operator key")
    dialog.get_by_label("Operator API key").fill(engine.client_key)
    dialog.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("heading", name="Operator access required")).to_be_visible()
    # Switch back to the operator key, then forget it.
    page.get_by_label("Operator API key").fill(engine.operator_key)
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("region", name="Signed-in identity")).to_contain_text("owner")
    page.get_by_role("button", name="Forget key").click()
    expect(page.get_by_role("heading", name="Operator access", exact=True)).to_be_visible()


def test_narrow_layout_has_no_horizontal_scroll_and_visible_focus(
    page: Page, engine: Engine
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, engine)
    for hash_ in ("#/overview", "#/models", "#/logs", "#/keys", "#/clients", "#/security"):
        page.goto(f"{engine.dashboard}{hash_}")
        page.wait_for_load_state("networkidle")
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"{hash_} scrolls horizontally by {overflow}px"
    # Keyboard focus on a navigation link is visibly indicated.
    link = page.get_by_role("link", name="Overview")
    link.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    outline = link.evaluate("el => getComputedStyle(el).outlineStyle")
    assert outline != "none"
