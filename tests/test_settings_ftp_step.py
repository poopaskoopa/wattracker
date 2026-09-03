"""The Settings FTP field must accept the fractional FTPs wattracker itself writes.

The field carried ``step="1"`` while ``_ftp_field_value`` deliberately echoes a
one-decimal stored FTP (its docstring: "the app itself produces one-decimal
FTPs, so the field has to be able to echo one back honestly").  A browser
enforces ``step`` before it will submit, so a rider whose recommended FTP was,
say, 219.4 could not submit the settings form AT ALL -- the whole form silently
refused, including unrelated fields like the time zone.  ``parse_ftp_input``
accepts any finite value in range, so ``step="any"`` is what matches the server.
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


def _ftp_input(text):
    match = re.search(r'<input[^>]*name="ftp"[^>]*>', text)
    return match.group(0) if match else None


def test_ftp_field_does_not_impose_a_whole_watt_step(client):
    _register(client)

    field = _ftp_input(client.get("/settings").text)

    assert field is not None
    assert 'step="any"' in field
    assert 'step="1"' not in field


def test_a_fractional_stored_ftp_is_echoed_into_the_field(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"ftp": 219.4})

    field = _ftp_input(client.get("/settings").text)

    assert 'value="219.4"' in field


def test_posting_a_fractional_ftp_saves_it_and_the_rest_of_the_form(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]

    response = client.post(
        "/settings", data={"ftp": "219.4", "timezone": "America/New_York"}
    )

    assert response.status_code == 200
    assert "FTP not saved" not in response.text
    settings = db.get_user_settings(uid)
    assert settings["ftp"] == pytest.approx(219.4)
    assert settings["timezone"] == "America/New_York"
