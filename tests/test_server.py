"""Smoke + auth + isolation tests for the FastAPI app."""
import datetime as dt

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.server import create_app  # noqa: E402
from wattracker.timeutil import utc_today  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="tester", password="password123"):
    return client.post(
        "/register", data={"username": username, "password": password}
    )


def _seed_activity(user_id, start_time="2026-06-01T10:00:00", watts=300.0, seconds=1200):
    db.insert_activity(
        user_id,
        {
            "dedup_hash": f"h-{user_id}-{start_time}",
            "filename": "a.fit",
            "start_time": start_time,
            "duration_s": seconds,
            "distance_m": 0.0,
            "avg_power": watts,
            "avg_hr": 0.0,
            "np": watts,
            "if_": 1.0,
            "tss": 100.0,
            "streams": {"power": [watts] * seconds},
        },
    )


# ------------------------------------------------------------- auth guard
def test_unauthenticated_root_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_unauthenticated_api_redirects_to_login(client):
    r = client.get("/api/state", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_and_register_pages_public(client):
    assert client.get("/login").status_code == 200
    assert client.get("/register").status_code == 200


# -------------------------------------------------------- authed pages
def test_register_then_dashboard(client):
    r = _register(client)  # follows redirect to dashboard
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "tester" in r.text  # username shown in nav


def test_authed_pages_and_api(client):
    _register(client)
    for path in ("/", "/activities", "/generate", "/profile", "/settings"):
        assert client.get(path).status_code == 200
    for path in ("/api/state", "/api/load", "/api/curve", "/api/activities", "/api/ftp"):
        r = client.get(path)
        assert r.status_code == 200
        r.json()


def test_dashboard_curve_selector_and_api_variants(client):
    _register(client)
    html = client.get("/").text
    assert 'id="curveSource"' in html
    assert "Last 90 days" in html and "All time" in html and "Last ride" in html
    curve = client.get("/api/curve").json()
    assert {"measured", "all_time", "last_ride"} <= curve.keys()


def test_settings_explains_training_and_recent_best_effort_ftp(client):
    _register(client)
    uid = db.get_user_by_username("tester")["id"]
    _seed_activity(uid, watts=300.0)
    text = client.get("/settings").text
    assert "Current Training FTP" in text
    assert "90-day best-effort FTP estimate" in text
    assert "285.0 W" in text
    assert "drives workout targets" in text
    assert "adjusts for" in text and "inactivity" in text
    assert "External and zFTP estimates may differ" in text


def test_dashboard_labels_training_ftp(client):
    r = _register(client)
    assert "Training FTP" in r.text


def test_generate_submit(client):
    _register(client)
    r = client.post("/generate", data={"duration_min": 60})
    assert r.status_code == 200
    assert "Est. TSS" in r.text or "Error" in r.text


def test_generate_invalid_duration_shows_error(client):
    _register(client)
    r = client.post("/generate", data={"duration_min": 10})
    assert r.status_code == 200
    assert "Error" in r.text


def test_logout_clears_session(client):
    _register(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/logout", follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303


# ----------------------------------------------------------- isolation
def test_per_user_data_isolation(client):
    # User A registers and gets data seeded.
    _register(client, "alice", "password123")
    a_id = db.get_user_by_username("alice")["id"]
    _seed_activity(a_id)
    db.add_ftp_entry(a_id, utc_today().isoformat(), 300.0, "manual")

    assert len(client.get("/api/activities").json()) == 1
    assert len(client.get("/api/ftp").json()) == 1
    assert client.get("/api/state").json()["ftp"] == pytest.approx(300.0)

    # Switch to a brand-new user B: independent, empty data.
    client.post("/logout")
    _register(client, "bob", "password123")
    assert client.get("/api/activities").json() == []
    assert client.get("/api/ftp").json() == []
    b_state = client.get("/api/state").json()
    assert b_state["ftp"] != pytest.approx(300.0)  # B does not see A's FTP

    # Back to A: data still present.
    client.post("/logout")
    client.post("/login", data={"username": "alice", "password": "password123"})
    assert len(client.get("/api/activities").json()) == 1
    assert client.get("/api/state").json()["ftp"] == pytest.approx(300.0)


# ------------------------------------------------------------- static asset cache-busting

def test_static_url_appends_mtime_version():
    from wattracker.server import static_url, _STATIC_DIR
    import os

    mtime = int(os.path.getmtime(os.path.join(_STATIC_DIR, "app.js")))
    assert static_url("app.js") == f"/static/app.js?v={mtime}"


def test_static_url_missing_file_has_no_version():
    from wattracker.server import static_url

    assert static_url("does-not-exist.js") == "/static/does-not-exist.js"


def test_dashboard_page_references_versioned_app_js(client):
    _register(client)
    r = client.get("/")
    assert "app.js?v=" in r.text


def test_static_response_has_no_cache_header(client):
    r = client.get("/static/app.js")
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "asset",
    ["chart.umd.min.js", "chartjs-plugin-zoom.umd.min.js"],
)
def test_vendored_chart_assets_are_served_and_referenced(client, asset):
    response = client.get(f"/static/vendor/{asset}")
    assert response.status_code == 200
    assert len(response.content) > 1000
    login = client.get("/login")
    assert f"/static/vendor/{asset}?v=" in login.text
    assert "cdn.jsdelivr.net" not in login.text


def test_settings_surfaces_secure_credential_storage_failure(client, monkeypatch):
    from wattracker import credstore

    _register(client)

    def fail(*_args):
        raise credstore.CredentialStorageError("vault unavailable")

    monkeypatch.setattr(credstore, "save_zwift_credentials", fail)
    response = client.post(
        "/settings",
        data={"zwift_email": "rider@example.com", "zwift_password": "secret123"},
    )
    assert response.status_code == 200
    assert "NOT saved securely" in response.text
    assert "vault unavailable" in response.text


def test_frozen_windows_settings_uses_embedded_restore_command(client, monkeypatch):
    import wattracker.server as server

    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(server.sys, "frozen", True, raising=False)
    monkeypatch.setattr(server.sys, "executable", r"C:\Program Files\wattracker\wattracker.exe")
    _register(client)
    response = client.get("/settings")
    assert response.status_code == 200
    assert "wattracker.exe restore" in response.text
    assert "wattracker-restore" not in response.text
