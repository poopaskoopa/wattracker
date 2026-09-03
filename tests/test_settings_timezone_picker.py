"""The Settings time zone control: a dropdown whose labels carry live offsets.

The field used to be free text asking for an IANA name, so nobody ever set it
and every calendar fell back to UTC. What is STORED is still the zone name -
these tests exist mostly to pin that the displayed offset is a per-request
display hint (it moves across a DST transition) while the stored value does
not change, and that a zone the generated list omits survives a round trip.
"""
import datetime as dt
import re

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker import server as servermod  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _option(text, zone):
    """The rendered <option> element for *zone*, or None."""
    match = re.search(
        r'<option value="%s"[^>]*>[^<]*</option>' % re.escape(zone), text
    )
    return match.group(0) if match else None


def _freeze(monkeypatch, moment):
    monkeypatch.setattr(servermod, "utc_now", lambda: moment)


def test_timezone_field_is_a_dropdown_not_a_text_input(client):
    _register(client)

    text = client.get("/settings").text

    assert '<select name="timezone">' in text
    assert 'name="timezone"' not in text.replace('<select name="timezone">', "")
    assert _option(text, "America/New_York") is not None
    assert _option(text, "UTC") is not None


def test_unset_timezone_preselects_utc(client):
    _register(client)

    option = _option(client.get("/settings").text, "UTC")

    assert "selected" in option


def test_option_labels_carry_the_current_offset(client, monkeypatch):
    # Expected offsets are written out literally rather than recomputed with
    # the helper the page uses, so this cannot pass by agreeing with a bug.
    _freeze(monkeypatch, dt.datetime(2026, 1, 15, 12, 0))
    _register(client)

    text = client.get("/settings").text

    assert "(UTC+00:00) UTC" in _option(text, "UTC")
    assert "(UTC+09:00) Asia/Tokyo" in _option(text, "Asia/Tokyo")


def test_same_zone_shows_different_offsets_across_a_dst_transition(
    client, monkeypatch
):
    # The point of storing an IANA name: New York is UTC-05:00 in January and
    # UTC-04:00 in July, with no action from the rider and no stored change.
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"timezone": "America/New_York"})

    _freeze(monkeypatch, dt.datetime(2026, 1, 15, 12, 0))
    winter = _option(client.get("/settings").text, "America/New_York")
    _freeze(monkeypatch, dt.datetime(2026, 7, 15, 12, 0))
    summer = _option(client.get("/settings").text, "America/New_York")

    assert "(UTC-05:00) America/New_York" in winter
    assert "(UTC-04:00) America/New_York" in summer
    assert "selected" in winter and "selected" in summer
    # The offset moved; what is stored did not.
    assert db.get_user_settings(uid)["timezone"] == "America/New_York"


def test_sub_hour_offsets_format_with_minutes(client, monkeypatch):
    _freeze(monkeypatch, dt.datetime(2026, 1, 15, 12, 0))
    _register(client)

    text = client.get("/settings").text

    assert "(UTC+05:30) Asia/Kolkata" in _option(text, "Asia/Kolkata")
    assert "(UTC+05:45) Asia/Kathmandu" in _option(text, "Asia/Kathmandu")
    assert "(UTC+08:45) Australia/Eucla" in _option(text, "Australia/Eucla")


def test_stored_legacy_alias_survives_a_round_trip(client, monkeypatch):
    # US/Eastern is a real zone the canonical Area/Location list omits. It must
    # still be offered, still be selected, and saving must not rewrite it.
    _freeze(monkeypatch, dt.datetime(2026, 1, 15, 12, 0))
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"timezone": "US/Eastern"})

    option = _option(client.get("/settings").text, "US/Eastern")
    assert option is not None
    assert "selected" in option
    assert "(UTC-05:00)" in option

    response = client.post("/settings", data={"timezone": "US/Eastern"})
    assert response.status_code == 200
    assert db.get_user_settings(uid)["timezone"] == "US/Eastern"


def test_garbage_zone_is_rejected_and_the_rest_of_the_form_still_saves(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"timezone": "Europe/Paris"})

    response = client.post(
        "/settings", data={"timezone": "Not/AZone", "zwift_id": "123456"}
    )

    assert response.status_code == 200
    assert "Time zone not saved" in response.text
    settings = db.get_user_settings(uid)
    assert settings["timezone"] == "Europe/Paris"
    assert settings["zwift_id"] == "123456"
