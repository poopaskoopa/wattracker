"""Reaching the server from a phone on the same wifi, while a connector rides.

The split install puts the server on a NAS and the connector on the Zwift
machine, and the obvious thing to want next is the ride screen on a phone
propped against the bars. Nothing in the code had to change for that to work -
but nothing pinned it either, and one doc claimed a part of it was impossible.
So this file is the evidence: a browser whose Host and Origin are a LAN name,
doing the things a rider does, with live watts arriving from a connector's
radio at the far end.

The controls it leans on are already tested next door in
test_network_posture.py; what is new here is the whole path at once.

Two things this deliberately does NOT assert, because they are properties of
an https reverse proxy rather than of a phone (see docs/calendar-feed.md):
guarded POSTs over a tailnet still 403, and that is the proxy changing the
scheme and port, not the device holding the browser.
"""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import connectorhub, db  # noqa: E402
from wattracker import server as servermod  # noqa: E402
from wattracker.server import create_app  # noqa: E402
from wattracker_connector import ble_handlers as blemod  # noqa: E402

from conftest_connector import FakeRadio, attach_connector  # noqa: E402
from conftest import _receive_until  # noqa: E402

# The name the phone would use, and the Host/Origin pair a browser sends with
# it. The port matters: _same_origin_or_absent compares it, and a Host header
# without one reads as port 80.
LAN = "wattracker.local"
HOST = f"{LAN}:8000"
PHONE = {"Origin": f"http://{HOST}", "Host": HOST}


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_hub():
    connectorhub.reset()
    yield
    connectorhub.reset()


@pytest.fixture(autouse=True)
def _lan(monkeypatch):
    """The server answers to a LAN name, as it must for any of this to work."""
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOSTS", LAN)


def _register(client, username="rider"):
    client.post(
        "/register", data={"username": username, "password": "password123"},
        headers=PHONE,
    )
    return db.get_user_by_username(username)["id"]


# --------------------------------------------------------- the ordinary app
def test_a_phone_can_register_and_load_pages(client):
    _register(client)
    for path in ("/", "/ride", "/settings"):
        response = client.get(path, headers={"Host": HOST}, follow_redirects=False)
        assert response.status_code == 200, path


def test_a_phone_can_press_a_button_that_changes_something(client):
    """The counterpart of the tailnet limitation, and the opposite result.

    Over a direct LAN bind the browser's Origin and the URL the server thinks
    it is serving agree on scheme, host and port, so the same-origin guard is
    satisfied and pairing a device from the phone works.
    """
    _register(client)
    response = client.post(
        "/settings/connector", data={"label": "Zwift PC"},
        headers=PHONE, follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Zwift PC" in response.text


def test_a_forged_host_is_still_refused(client):
    """Widening to one LAN name must not widen to any name."""
    _register(client)
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


# -------------------------------------------------------------- the ride
def test_a_phone_watches_a_ride_the_connector_is_actually_riding(
    client, tmp_path, monkeypatch
):
    """The whole point, end to end.

    A real /connector/ws socket, the real RPC framing, the real BLE handlers -
    against a fake radio - and a browser on a LAN name reading the frames that
    come out the other side. The numbers asserted are the ones the radio was
    told to report, so they can only arrive by having crossed the connector.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.005)
    uid = _register(client)
    radio = FakeRadio(power=213, cadence=91.0, hr=147)
    attached, _config = attach_connector(client, uid, tmp_path, radio=radio)

    with attached:
        frames = []
        running = []
        with client.websocket_connect(
            "/ride/ws?type=endurance&minutes=30", headers=PHONE,
        ) as ws:
            def _received(message):
                frames.append(message)
                status = message.get("status")
                assert status not in ("error", "unavailable"), message
                return status == "connected"

            _receive_until(ws, _received, "a 'connected' frame")

            def _running(message):
                if message.get("status") == "running":
                    running.append(message)
                return len(running) >= 3

            _receive_until(ws, _running, "3 'running' frames")

    assert frames[0]["status"] == "workout"
    # Live, and from the connector: these are the fake radio's readings.
    assert running[-1]["power"] == 213
    assert running[-1]["cadence"] == 91.0
    assert running[-1]["hr"] == 147
    assert running[-1]["erg_available"] is True


def test_the_ride_socket_still_refuses_a_page_it_did_not_serve(client, tmp_path):
    """A LAN name in the allowlist is not a hole for every other origin."""
    from starlette.websockets import WebSocketDisconnect

    _register(client)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/ride/ws?sim=1",
            headers={"Origin": "http://evil.example", "Host": HOST},
        ) as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008
