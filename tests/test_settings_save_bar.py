"""The Settings page's sticky save bar.

The Save button used to be the last element of a form several screens tall and
looked exactly like the five other buttons beneath it, so the owner changed a
setting near the top of the page and reported "I don't see any way to save my
choice". These tests pin the three things that make the fix real rather than
cosmetic: the control is still a plain submit tied to the settings form, the
POST it sends still saves, and a rejected value's explanation - which renders
at the TOP of the page, potentially screens away from the bar - is reachable
from the bar without hunting for it.
"""
import re

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _save_bar(text):
    """The rendered save-bar markup, or None."""
    match = re.search(
        r'<div class="settings-save-bar">.*?</div>\s*</div>', text, re.S)
    return match.group(0) if match else None


def test_the_save_control_renders_inside_the_settings_form(client):
    _register(client)

    text = client.get("/settings").text
    bar = _save_bar(text)

    assert bar is not None, "no save bar on the Settings page"
    assert 'id="settings-save"' in bar
    assert 'type="submit"' in bar
    # Nested inside the form that POSTs to /settings, so the association is the
    # HTML one and needs no script. (A `form=` attribute would do as well; what
    # must not happen is a button wired up in JavaScript.)
    form_start = text.index(
        '<form method="post" action="/settings" class="settings-form"')
    form_end = text.index("</form>", form_start)
    assert form_start < text.index(bar) < form_end, (
        "the save bar is outside the settings form and carries no form= "
        "attribute, so it submits nothing")


def test_the_settings_form_has_an_id_the_bar_can_be_tied_to(client):
    _register(client)

    assert 'id="settings-form"' in client.get("/settings").text


def test_the_save_button_outweighs_the_other_buttons_on_the_page(client):
    _register(client)
    css = client.get("/static/style.css").text

    assert ".settings-save-btn" in css
    # Fixed to the viewport bottom, so it is on screen at any scroll position.
    bar_rules = css[css.index(".settings-save-bar {"):]
    assert "position: fixed" in bar_rules[:400]
    assert "bottom: 0" in bar_rules[:400]
    # And the flow reserves its height, so nothing sits under it permanently.
    assert "main.has-save-bar { padding-bottom: var(--save-bar-h); }" in css
    assert "main.has-save-bar ~ footer" in css


def test_the_page_asks_for_the_bottom_padding_the_bar_needs(client):
    _register(client)

    # The <main> class is what the padding rule hangs off; without it the last
    # button on the page sits under the bar forever.
    assert 'class="has-save-bar"' in client.get("/settings").text


def test_submitting_through_the_form_still_saves(client):
    _register(client)

    client.post(
        "/settings",
        data={"ftp": "250", "zwift_id": "12345", "weight_kg": "70",
              "timezone": "Asia/Tokyo"},
    )

    uid = db.get_user_by_username("rider")["id"]
    settings = db.get_user_settings(uid)
    assert settings["timezone"] == "Asia/Tokyo"
    assert settings["zwift_id"] == "12345"
    # And the page comes back showing what was stored, with the bar still on it.
    text = client.get("/settings").text
    assert _save_bar(text) is not None
    assert re.search(r'<option value="Asia/Tokyo"[^>]*selected', text)


def test_a_rejected_value_is_reachable_from_the_bar(client):
    """A rider submitting from the bottom must be told why nothing saved.

    The alerts render at the top of the page. The bar therefore carries a link
    to them whenever one is present - that is the no-JavaScript path - and the
    page scrolls them into view on load.
    """
    _register(client)

    text = client.post(
        "/settings", data={"ftp": "250", "timezone": "Mars/Olympus"}).text
    bar = _save_bar(text)

    assert 'id="settings-alerts"' in text
    assert '<p class="alert" role="alert">' in text
    assert bar is not None
    assert 'href="#settings-alerts"' in bar, (
        "a rejected value left the save bar silent; the rider sees the button "
        "do nothing and the reason sits off-screen above them")
    # The alert paragraph really is inside the anchored block.
    block = re.search(r'<div id="settings-alerts">.*?</div>', text, re.S).group(0)
    assert 'class="alert"' in block
    # And the scroll-into-view is wired to that same id.
    assert 'getElementById("settings-alerts")' in text


def test_a_clean_save_leaves_the_bar_free_of_the_alert_link(client):
    _register(client)

    text = client.post(
        "/settings", data={"ftp": "250", "timezone": "Asia/Tokyo"}).text

    assert 'href="#settings-alerts"' not in _save_bar(text)
