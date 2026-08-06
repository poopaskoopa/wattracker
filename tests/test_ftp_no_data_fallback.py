"""Issue #55: the no-data FTP fallback must not masquerade as an analysis.

When a rider finished onboarding with no FIT data, the wizard recorded a
fabricated 200 W row as ``source='estimated'`` - a number nobody measured,
stored where every later reader (the dashboard, the exporter, the offline
rescore in #59) sees an analysed FTP. That is the same class of defect as
#44/#54/#60: an invented value made indistinguishable from a real one.

The literal was also repeated - ``importer.DEFAULT_FTP``, two ``200`` in
server.py's setup contexts, and the wizard template - so the displayed
fallback, the stored history and ``current_ftp``'s fallback could drift.

The policy these tests pin:

* one definition, ``wattracker.metrics.power.DEFAULT_FTP``;
* it is NEVER written to ftp_history - it is resolved at read time by
  ``current_ftp`` when there is genuinely nothing to resolve;
* so ftp_history contains only measurements and rider assertions, and the
  first real ride writes the first row.
"""
import datetime as dt

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import wattracker.ingest.importer as importer
from wattracker import db
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


def _parsed(start_time, seconds=3600, watts=250.0):
    return {
        "start_time": start_time,
        "duration_s": seconds,
        "streams": {
            "time": [None] * seconds,
            "power": [watts] * seconds,
            "heartrate": [140.0] * seconds,
            "cadence": [90.0] * seconds,
            "distance": list(range(seconds)),
            "altitude": [0.0] * seconds,
        },
    }


# ------------------------------------------------------------- one definition

def test_the_no_data_placeholder_has_exactly_one_definition():
    """FAILS without the fix: power.DEFAULT_FTP did not exist, and the wizard
    context carried its own ``200`` literal alongside importer's."""
    from wattracker.metrics.power import DEFAULT_FTP

    assert importer.DEFAULT_FTP is DEFAULT_FTP
    assert DEFAULT_FTP == 200.0


# ------------------------------------------------- empty-data onboarding (#55)

def test_empty_data_onboarding_records_no_fabricated_history(client):
    """FAILS without the fix: a 200 W source='estimated' row was written for a
    rider who had imported nothing and been analysed for nothing."""
    uid = _register(client)

    response = client.post("/setup/complete", data={
        "weight_kg": "72", "ftp_choice": "estimated", "zwiftpower": "no",
    })

    assert response.status_code == 200
    assert db.onboarding_complete(uid) is True
    assert db.latest_ftp(uid) is None, "an invented FTP was recorded as history"
    assert db.get_user_settings(uid)["ftp"] is None


def test_the_setup_endpoint_calls_a_placeholder_a_placeholder(client):
    """FAILS without the fix: the reply said source='estimated' and the same
    invented number was persisted."""
    uid = _register(client)

    payload = client.post("/setup/ftp", data={"choice": "estimated"}).json()

    assert payload["ftp"] == pytest.approx(importer.DEFAULT_FTP)
    assert payload["source"] == "default"
    assert db.latest_ftp(uid) is None


def test_a_superseded_manual_row_goes_away_with_nothing_to_replace_it(client):
    """FAILS without the fix: the manual row was replaced by a fabricated
    estimate, so the rider's history showed a measurement they never made.

    Choosing the analysed estimate must clear the manual value the rider is
    moving away from - and when there is no analysis, the replacement is
    nothing, not an invention. current_ftp then agrees with what the wizard
    just told the rider.
    """
    uid = _register(client)
    assert client.post(
        "/setup/ftp", data={"choice": "manual", "manual_ftp": "275"}
    ).status_code == 200
    assert db.latest_ftp(uid)["ftp_watts"] == pytest.approx(275.0)

    payload = client.post("/setup/ftp", data={"choice": "estimated"}).json()

    assert db.latest_ftp(uid) is None
    assert db.get_user_settings(uid)["ftp"] is None
    assert importer.current_ftp(uid) == pytest.approx(payload["ftp"])


def test_current_ftp_and_the_wizard_agree_when_there_is_no_data(client):
    """Mostly a guard: the two numbers were both 200 before the fix as well.

    Only its second assertion - that the wizard calls the number a placeholder
    rather than "the FIT-derived estimate" - fails against the unfixed page.
    The number check is here because the two used to be independent literals,
    which is the drift #55 is about; it fails the moment they part company.
    """
    uid = _register(client)

    page = client.get("/setup")

    assert page.status_code == 200
    # Deliberately asserted on the NUMBER, not on the wording, so this stays a
    # true either-way guard rather than a test of this commit's copy edit.
    assert f"{round(importer.current_ftp(uid))} W" in page.text
    assert "placeholder" in page.text


# ------------------------------------------- and a real ride is still an estimate

def test_a_later_real_ride_writes_the_first_genuine_history_row(
    client, tmp_path, monkeypatch
):
    """FAILS without the fix: history was already non-empty before the ride,
    holding an 'estimated' 200 W nothing had estimated.

    The point of not writing the placeholder is that ftp_history stays a record
    of real evaluations - so the estimator's first genuine result is also the
    rider's first row, and current_ftp switches to it without anything having
    to overwrite an invention.
    """
    uid = _register(client)
    assert client.post("/setup/complete", data={
        "weight_kg": "72", "ftp_choice": "estimated", "zwiftpower": "no",
    }).status_code == 200
    assert db.latest_ftp(uid) is None
    assert importer.current_ftp(uid) == pytest.approx(importer.DEFAULT_FTP)

    activities = tmp_path / "Activities"
    activities.mkdir()
    (activities / "ride.fit").write_bytes(b"fit")
    start = importer.utc_now() - dt.timedelta(days=1)
    monkeypatch.setattr(
        importer, "parse_fit",
        lambda path: _parsed(start.replace(microsecond=0).isoformat(), 3600, 250.0),
    )
    assert importer.scan_activities(uid, str(activities))["imported"] == 1

    rows = db.ftp_history_list(uid)
    latest = db.latest_ftp(uid)
    assert latest is not None
    assert latest["source"] == "estimated"
    assert latest["date"] == utc_today().isoformat()
    assert latest["ftp_watts"] > importer.DEFAULT_FTP  # a real 250 W hour
    assert importer.current_ftp(uid) == pytest.approx(latest["ftp_watts"])
    assert len(rows) == 1, f"placeholder left behind alongside the estimate: {rows}"
