"""Losing operator access mid-session, in a real browser against the real engine.

A disposable operator key, created for this test only, signs in to the
dashboard. The suite's owner key (never used in the browser here) then revokes
it. The dashboard's next protected request fails with 401, and the dashboard
must end the session: authenticated content disappears and the key prompt
returns with the "not accepted" message. The owner key is never revoked, so the
order in which tests run does not matter.

The disposable token is masked in the Actions log, typed only into the key
field, and no screenshot is taken while it is on screen.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from e2e.conftest import Engine, create_operator_key, login, purge_key


def test_revoked_key_ends_the_dashboard_session(page: Page, engine: Engine) -> None:
    key = create_operator_key(engine, "e2e-disposable")
    try:
        login(page, engine, key=key.token)
        identity = page.get_by_role("region", name="Signed-in identity")
        expect(identity).to_contain_text(key.label)
        # The Keys view does not poll, so nothing is requested between the
        # revocation and the explicit refresh below.
        page.goto(f"{engine.dashboard}#/keys")
        heading = page.get_by_role("heading", name="API keys", exact=True)
        expect(heading).to_be_visible()
        expect(page.get_by_text(key.label, exact=True).first).to_be_visible()

        with engine.api(engine.operator_key) as c:
            resp = c.delete(f"/admin/keys/{key.id}")
            assert resp.status_code == 200, resp.status_code
        with engine.api(key.token) as c:
            assert c.get("/admin/identity").status_code == 401

        # A protected refresh (the key list) now fails with 401.
        page.get_by_role("button", name="Refresh", exact=True).click()

        expect(page.get_by_role("heading", name="Operator access", exact=True)).to_be_visible()
        expect(page.get_by_role("alert")).to_contain_text("not accepted")
        expect(heading).to_have_count(0)
        expect(page.get_by_role("region", name="Signed-in identity")).to_have_count(0)
        expect(page.get_by_role("navigation", name="Primary")).to_have_count(0)
        assert key.label not in page.content()

        # The owner key still works: signing in again starts a fresh session.
        page.get_by_label("Operator API key").fill(engine.operator_key)
        page.get_by_role("button", name="Continue").click()
        expect(page.get_by_role("region", name="Signed-in identity")).to_contain_text("owner")
    finally:
        purge_key(engine, key.id)
