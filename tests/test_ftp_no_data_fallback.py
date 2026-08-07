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


def test_setup_endpoint_does_not_report_a_rejected_history_source(client):
    """A rejected history row cannot lend its provenance to DEFAULT_FTP."""
    uid = _register(client)
    db.add_ftp_entry(uid, utc_today().isoformat(), 0.64, "estimated")

    payload = client.post("/setup/ftp", data={"choice": "estimated"}).json()

    assert payload["ftp"] == pytest.approx(importer.DEFAULT_FTP)
    assert payload["source"] == "default"
    assert importer.current_ftp(uid) == pytest.approx(payload["ftp"])


def test_a_superseded_manual_row_is_not_replaced_by_an_invention(client):
    """FAILS without the fix: the manual row was replaced by a fabricated
    estimate, so the rider's history showed a measurement they never made.

    That property is the point of #55 and is unchanged. What *did* change:
    this test used to also assert the manual row was deleted, leaving
    ``latest_ftp`` None. Review found that deletion could destroy history the
    wizard never created - the same code path removes a value typed into
    Settings, or a real estimate from a ride logged this morning, because a row
    keyed on (user, today) cannot be told apart by who wrote it.

    So nothing is written and nothing is deleted. The rider keeps their own
    most recent assertion, which is a better answer than a generic placeholder
    when there is genuinely no analysis, and the override is still cleared so
    the estimate can take over the moment one exists.
    """
    uid = _register(client)
    assert client.post(
        "/setup/ftp", data={"choice": "manual", "manual_ftp": "275"}
    ).status_code == 200
    assert db.latest_ftp(uid)["ftp_watts"] == pytest.approx(275.0)

    payload = client.post("/setup/ftp", data={"choice": "estimated"}).json()

    # No invention: nothing was written claiming to be an analysis.
    rows = db.ftp_history_list(uid)
    assert [r["source"] for r in rows] == ["manual"]
    assert rows[0]["ftp_watts"] == pytest.approx(275.0)
    # The override is cleared, so a real estimate will win as soon as one lands.
    assert db.get_user_settings(uid)["ftp"] is None
    # And the wizard reports what the rider will actually get.
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
    assert f"{float(importer.current_ftp(uid)):g} W" in page.text
    assert "placeholder" in page.text


@pytest.mark.parametrize(
    ("watts", "source", "claim"),
    [(262.0, "estimated", "262 W"), (275.0, "manual", "manual FTP")],
)
def test_setup_wizard_describes_the_ftp_current_ftp_will_use(
    client, watts, source, claim
):
    """The wizard must not call a real current FTP a fallback placeholder."""
    uid = _register(client, f"wizard-{source}")
    db.add_ftp_entry(uid, utc_today().isoformat(), watts, source)

    page = client.get("/setup")

    assert page.status_code == 200
    assert f"{float(importer.current_ftp(uid)):g} W" in page.text
    assert claim in page.text
    assert "placeholder" not in page.text


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


def test_the_wizard_never_deletes_a_same_day_row_it_did_not_create(client):
    """The wizard must not destroy FTP history the rider produced.

    Choosing "estimated" with no analysis available used to remove *any*
    ftp_history row for today, to stop a superseded value contradicting the
    placeholder the page had just shown. But a row for today is the rider's
    own: a value they typed into Settings, or a real estimate from a ride they
    logged this morning. Deleting it to make room for a placeholder destroys
    data the wizard did not create - and the placeholder is not even written,
    so the deletion bought nothing but the loss.

    Now nothing is deleted, and the response reports what current_ftp will
    actually resolve rather than a number that contradicts it.
    """
    uid = _register(client)
    today = utc_today().isoformat()
    db.add_ftp_entry(uid, today, 275.0, "manual")

    response = client.post("/setup/ftp", data={"choice": "estimated"})
    assert response.status_code == 200

    rows = db.ftp_history_list(uid)
    assert len(rows) == 1, f"the rider's own row was destroyed: {rows}"
    assert rows[0]["ftp_watts"] == pytest.approx(275.0)
    assert rows[0]["source"] == "manual"
    # And the reply agrees with what the rider will actually get.
    assert response.json()["ftp"] == pytest.approx(275.0)
    assert importer.current_ftp(uid) == pytest.approx(275.0)


def test_completing_setup_without_an_analysis_keeps_real_history(client, monkeypatch):
    """Same protection on the /setup/complete path, which had the same delete."""
    uid = _register(client)
    today = utc_today().isoformat()
    db.add_ftp_entry(uid, today, 262.0, "estimated")

    response = client.post(
        "/setup/complete",
        data={
            "weight_kg": "72", "ftp_choice": "estimated", "zwiftpower": "no",
        },
    )
    assert response.status_code in (200, 303)

    rows = db.ftp_history_list(uid)
    assert len(rows) == 1, f"a measured estimate was destroyed: {rows}"
    assert rows[0]["ftp_watts"] == pytest.approx(262.0)
    assert rows[0]["source"] == "estimated"
