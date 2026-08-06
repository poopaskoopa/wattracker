"""Issue #64: one validation policy for every surface a rider types an FTP into.

Before this, ``/settings`` passed its form field straight into the database:
``ftp=1`` stored 1.0, ``ftp=0.64`` stored 0.64 (the exact #60 basis, reachable
through a form), ``ftp=-5`` stored -5.0 and ``ftp=abc`` stored the *string*
'abc' into a column every FTP consumer treats as a number. Meanwhile
``/setup/*`` bounded the same field 1-1000 W and ``/profile/ftp`` 1-2000 W, so
the least-guarded surface was the main Settings page.

A stored FTP is a scoring basis and TSS is quadratic in 1/FTP, so this is not a
cosmetic input bug: an FTP of 1 W scores an ordinary hour at TSS 4,000,000.

What must NOT regress (issue #60/#67): a rider's *asserted* sub-floor FTP is
still honoured as a scoring basis, end to end. The input layer challenges it
once, and a confirmed assertion is used exactly as entered - see
``test_a_confirmed_sub_floor_ftp_is_honoured_end_to_end``.
"""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import wattracker.ingest.importer as importer
from wattracker import db
from wattracker.metrics.power import is_plausible_ftp
from wattracker.server import create_app
from wattracker.timeutil import utc_today


@pytest.fixture()
def client():
    with TestClient(create_app()) as value:
        yield value


def _register(client, username="rider"):
    assert client.post(
        "/register", data={"username": username, "password": "password123"}
    ).status_code == 200
    return db.get_user_by_username(username)["id"]


def _stored_ftp(uid):
    return db.get_user_settings(uid)["ftp"]


# --------------------------------------------------------------- the policy

def test_the_input_window_is_the_window_a_basis_may_be_scored_in():
    """A guard: it pins WHY the bounds are these (the module is new, so there
    is no unfixed code for it to fail against).

    Admitting an input the scorer will later refuse stores a number that is
    echoed back as the rider's setting and silently scores nothing, so the
    input window is exactly ``FTP_ASSERTION_MIN/MAX_WATTS``.
    """
    from wattracker.ftp_input import (
        FTP_CONFIRM_BELOW_WATTS,
        FTP_INPUT_MAX_WATTS,
        FTP_INPUT_MIN_WATTS,
        parse_ftp_input,
    )

    assert (FTP_INPUT_MIN_WATTS, FTP_INPUT_MAX_WATTS) == (20.0, 700.0)
    assert FTP_CONFIRM_BELOW_WATTS == 50.0
    assert parse_ftp_input("275").watts == 275.0
    assert parse_ftp_input("250.5").watts == 250.5
    assert parse_ftp_input("20").watts is None      # possible, but challenged
    assert parse_ftp_input("20", confirmed=True).watts == 20.0
    assert parse_ftp_input("19.9", confirmed=True).watts is None
    assert parse_ftp_input("701", confirmed=True).watts is None
    assert parse_ftp_input("abc").watts is None
    assert parse_ftp_input("").watts is None
    assert parse_ftp_input("nan").watts is None
    assert parse_ftp_input(True).watts is None
    # Only a sub-floor value is confirmable; a typo is refused outright.
    assert parse_ftp_input("40").needs_confirmation is True
    assert parse_ftp_input("4").needs_confirmation is False


# ------------------------------------------------------------ /settings (#64)

def test_settings_refuses_a_non_numeric_ftp(client):
    """FAILS without the fix: the string 'abc' was stored in a numeric column."""
    uid = _register(client)

    response = client.post("/settings", data={"ftp": "abc"})

    assert response.status_code == 200
    assert _stored_ftp(uid) is None
    assert db.latest_ftp(uid) is None
    assert "FTP not saved" in response.text


@pytest.mark.parametrize("value", ["1", "0.64", "0.5", "-5", "3000", "0"])
def test_settings_refuses_an_ftp_outside_the_human_range(client, value):
    """FAILS without the fix: every one of these was stored verbatim.

    0.64 is the #60 basis; 3000 is the fat-finger that stores a sixth of a
    ride's true load forever; 3.2 W/kg typed into a watts field lands here too.
    """
    uid = _register(client, f"rider{value.replace('.', '').replace('-', 'n')}")

    response = client.post("/settings", data={"ftp": value})

    assert _stored_ftp(uid) is None
    assert db.latest_ftp(uid) is None
    assert "FTP not saved" in response.text


def test_settings_saves_the_rest_of_the_form_when_the_ftp_is_refused(client):
    """A refused FTP must not discard the fields the rider got right."""
    uid = _register(client)

    client.post("/settings", data={"ftp": "abc", "timezone": "Europe/Paris"})

    settings = db.get_user_settings(uid)
    assert settings["ftp"] is None
    assert settings["timezone"] == "Europe/Paris"


def test_settings_still_stores_an_ordinary_ftp(client):
    """A guard: it passes either way. The fix must not break the normal path."""
    uid = _register(client)

    client.post("/settings", data={"ftp": "245"})

    assert _stored_ftp(uid) == pytest.approx(245.0)
    latest = db.latest_ftp(uid)
    assert latest["source"] == "manual"
    assert latest["ftp_watts"] == pytest.approx(245.0)


# -------------------------------------------------- one policy, three surfaces

@pytest.mark.parametrize("value", ["999", "1500", "0.64", "abc"])
def test_no_entry_point_is_more_permissive_than_another(client, value):
    """FAILS without the fix on every surface for at least one of these values.

    999 was accepted by the wizard (1-1000) and by /profile (1-2000) and stored
    unbounded by /settings; 0.64 and 'abc' were accepted by /settings alone.
    """
    uid = _register(client, f"rider{value.replace('.', '')}")

    settings = client.post("/settings", data={"ftp": value})
    assert _stored_ftp(uid) is None, f"/settings stored {value}"
    assert "FTP not saved" in settings.text

    profile = client.post("/profile/ftp", data={"ftp": value, "action": "save"})
    assert profile.status_code == 200 and 'role="alert"' in profile.text
    assert _stored_ftp(uid) is None, f"/profile/ftp stored {value}"

    wizard = client.post(
        "/setup/ftp", data={"choice": "manual", "manual_ftp": value}
    )
    assert wizard.status_code == 400, f"/setup/ftp accepted {value}"
    assert _stored_ftp(uid) is None

    complete = client.post("/setup/complete", data={
        "weight_kg": "72", "ftp_choice": "manual", "manual_ftp": value,
        "zwiftpower": "no",
    })
    assert complete.status_code == 400, f"/setup/complete accepted {value}"
    assert _stored_ftp(uid) is None
    assert db.latest_ftp(uid) is None


@pytest.mark.parametrize("route,data", [
    ("/settings", {"ftp": "275"}),
    ("/profile/ftp", {"ftp": "275", "action": "save"}),
    ("/setup/ftp", {"choice": "manual", "manual_ftp": "275"}),
    ("/setup/complete", {"weight_kg": "72", "ftp_choice": "manual",
                         "manual_ftp": "275", "zwiftpower": "no"}),
])
def test_every_entry_point_still_accepts_a_normal_ftp(client, route, data):
    """A guard: passes either way. The unified policy must reject nothing real."""
    uid = _register(client, "rider" + route.replace("/", "_"))

    response = client.post(route, data=data, follow_redirects=False)

    assert response.status_code in (200, 303)
    assert _stored_ftp(uid) == pytest.approx(275.0)


# ----------------------------------------------- the sub-floor confirmation

def test_a_sub_floor_ftp_is_challenged_before_it_is_stored(client):
    """FAILS without the fix: 40 W was stored silently, as was the typo 4 W.

    40 W is a real rehab rider; 4 W is a typo, a W/kg value, or a decimal slip.
    The first is confirmable, the second is not.
    """
    uid = _register(client)

    challenged = client.post("/settings", data={"ftp": "40"})

    assert _stored_ftp(uid) is None
    assert "confirm_low_ftp" in challenged.text
    assert 'value="40"' in challenged.text  # the rider's entry is echoed back

    refused = client.post(
        "/settings", data={"ftp": "4", "confirm_low_ftp": "on"}
    )

    assert _stored_ftp(uid) is None
    assert "confirm_low_ftp" not in refused.text  # nothing to confirm


def test_a_confirmed_sub_floor_ftp_is_honoured_end_to_end(client, monkeypatch):
    """The #60/#67 rehab case, driven through the real route.

    A guard: it passes either way (the unfixed code stored 40 W too, just
    without ever asking). It is here because it is the behaviour the whole
    confirmation design exists to preserve - a rider who confirms 40 W gets
    40 W as their scoring basis, not a floor and not a default - and it is the
    thing a future tightening of the input policy would silently break.
    """
    uid = _register(client)

    saved = client.post("/settings", data={"ftp": "40", "confirm_low_ftp": "on"})

    assert "FTP not saved" not in saved.text
    assert _stored_ftp(uid) == pytest.approx(40.0)
    latest = db.latest_ftp(uid)
    assert latest["source"] == "manual" and latest["ftp_watts"] == pytest.approx(40.0)
    assert importer.current_ftp(uid) == pytest.approx(40.0)
    assert is_plausible_ftp(importer.current_ftp(uid)) is True


# ------------------------------------------------- the write-side type guard

def test_save_user_settings_refuses_a_non_numeric_ftp():
    """FAILS without the fix: 'abc' reached the column whatever the caller was.

    The route validates too, but the guard has to hold for every caller - a
    migration, a script, a future route.
    """
    db.init_db()
    db.create_user("dbguard", "password123")
    uid = db.get_user_by_username("dbguard")["id"]

    db.save_user_settings(uid, {"ftp": "abc"})
    assert db.get_user_settings(uid)["ftp"] is None

    db.save_user_settings(uid, {"ftp": [250]})
    assert db.get_user_settings(uid)["ftp"] is None

    db.save_user_settings(uid, {"ftp": float("inf")})
    assert db.get_user_settings(uid)["ftp"] is None

    # A numeric string is coerced, not rejected: it is a number.
    db.save_user_settings(uid, {"ftp": "250"})
    assert db.get_user_settings(uid)["ftp"] == pytest.approx(250.0)


def test_the_write_guard_is_a_type_guard_and_not_a_range_check():
    """A guard: passes either way, and pins that #60's rails still own range.

    ``db`` deliberately still stores an out-of-range number - it is inert, and
    ``current_ftp``/``is_plausible_ftp`` are what refuse to score it. Turning
    this into a range check would move the #60 decision into the wrong layer and
    silently break the rehab rider's 40 W.
    """
    db.init_db()
    db.create_user("rangeguard", "password123")
    uid = db.get_user_by_username("rangeguard")["id"]

    db.save_user_settings(uid, {"ftp": 0.64})
    assert db.get_user_settings(uid)["ftp"] == pytest.approx(0.64)
    assert importer.current_ftp(uid) == importer.DEFAULT_FTP

    db.save_user_settings(uid, {"ftp": 40.0})
    db.add_ftp_entry(uid, utc_today().isoformat(), 40.0, "manual")
    assert importer.current_ftp(uid) == pytest.approx(40.0)
