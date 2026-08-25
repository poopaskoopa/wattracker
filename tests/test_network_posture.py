"""Binding beyond loopback, and the allowlists that have to widen with it.

Every control in this app was written assuming a loopback bind. These tests
pin the two things that make relaxing it deliberate rather than accidental:
the opt-out is a separate variable, and widening the Host allowlist does not
widen what any single entry may be.
"""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import config, connectorauth, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402


# --------------------------------------------------------------- the bind
def test_loopback_is_the_default(monkeypatch):
    monkeypatch.delenv("WATTRACKER_HOST", raising=False)
    assert config.server_host() == "127.0.0.1"
    assert config.allow_non_loopback() is False


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_non_loopback_is_refused_without_the_opt_in(monkeypatch, host):
    monkeypatch.setenv("WATTRACKER_HOST", host)
    monkeypatch.delenv("WATTRACKER_ALLOW_NON_LOOPBACK", raising=False)
    with pytest.raises(ValueError, match="loopback-only"):
        config.server_host()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_non_loopback_is_allowed_with_the_opt_in(monkeypatch, host):
    monkeypatch.setenv("WATTRACKER_HOST", host)
    monkeypatch.setenv("WATTRACKER_ALLOW_NON_LOOPBACK", "1")
    assert config.server_host() == host


def test_the_opt_in_alone_changes_nothing(monkeypatch):
    """It permits a wider bind; it does not choose one."""
    monkeypatch.setenv("WATTRACKER_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.delenv("WATTRACKER_HOST", raising=False)
    assert config.server_host() == "127.0.0.1"


# ------------------------------------------------------------ public hosts
def test_public_hosts_accepts_several(monkeypatch):
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOST", raising=False)
    monkeypatch.setenv(
        "WATTRACKER_PUBLIC_HOSTS", "wattracker.local, 192.168.1.10:8000 ,nas"
    )
    assert config.public_hosts() == ["wattracker.local", "192.168.1.10:8000", "nas"]


def test_public_hosts_folds_in_the_single_setting_without_duplicating(monkeypatch):
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", "nas.tail1234.ts.net")
    monkeypatch.setenv(
        "WATTRACKER_PUBLIC_HOSTS", "nas.tail1234.ts.net,wattracker.local"
    )
    assert config.public_hosts() == ["nas.tail1234.ts.net", "wattracker.local"]


def test_public_hosts_is_empty_by_default(monkeypatch):
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOST", raising=False)
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOSTS", raising=False)
    assert config.public_hosts() == []


@pytest.mark.parametrize(
    "bad",
    [
        "*",                      # wildcards were never accepted and still are not
        "*.example.com",
        "http://host",            # scheme
        "host/path",              # path
        "user@host",              # userinfo
        "host:notaport",
        "host:0",
        "host:99999",
        "[::1]",                  # IPv6 literal
        "host.",                  # trailing dot -> empty label
        "ho st",                  # whitespace
        "hosт",              # non-ASCII lookalike
    ],
)
def test_a_bad_entry_is_refused_the_same_in_the_list_form(monkeypatch, bad):
    """Widening the count must not widen what one entry may be."""
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOST", raising=False)
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOSTS", f"good.local,{bad}")
    with pytest.raises(ValueError):
        config.public_hosts()


# ------------------------------------------------------------- allowlists
def test_a_configured_host_is_accepted_and_others_are_not(monkeypatch):
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOSTS", "wattracker.local")
    with TestClient(create_app()) as client:
        ok = client.get("/login", headers={"Host": "wattracker.local"})
        assert ok.status_code == 200
        bad = client.get("/login", headers={"Host": "evil.example"})
        assert bad.status_code == 400


def test_a_connector_cannot_dial_a_name_the_server_does_not_answer_to(monkeypatch):
    """The trap behind "I paired it and it still will not connect".

    A connector dials ``ws://<server>:8000/connector/ws``, and that handshake
    carries the address it dialled as its ``Host``. The Host allowlist covers
    websocket scopes as well as http ones, so an unlisted address is refused
    *before* the bearer token is looked at - a perfectly valid token, refused
    for a reason that has nothing to do with the token. Pairing stores only a
    label, so nothing about this can be fixed from the pairing page.
    """
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOSTS", raising=False)
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOST", raising=False)
    with TestClient(create_app()) as client:
        client.post(
            "/register", data={"username": "rider", "password": "password123"}
        )
        uid = db.get_user_by_username("rider")["id"]
        _device_id, token = connectorauth.generate_token(uid, "Zwift PC")

        with pytest.raises(Exception) as excinfo:
            with client.websocket_connect(
                "/connector/ws",
                headers={"Authorization": f"Bearer {token}",
                         "Host": "192.168.1.10:8000"},
            ) as ws:
                ws.receive_json()
        assert "WebSocketDenial" in type(excinfo.value).__name__


def test_naming_the_server_is_what_lets_that_same_token_in(monkeypatch):
    """Same token, same Host, one entry added: the connector attaches."""
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOSTS", "192.168.1.10")
    with TestClient(create_app()) as client:
        client.post(
            "/register", data={"username": "rider", "password": "password123"}
        )
        uid = db.get_user_by_username("rider")["id"]
        _device_id, token = connectorauth.generate_token(uid, "Zwift PC")

        with client.websocket_connect(
            "/connector/ws",
            headers={"Authorization": f"Bearer {token}",
                     "Host": "192.168.1.10:8000"},
        ) as ws:
            assert ws.receive_json()["event"] == "hello"


def test_the_ride_socket_accepts_its_own_page_over_a_lan_name(monkeypatch):
    """Served over a LAN name, the ride page's Origin *is* that name.

    An allowlist of only localhost would refuse the very page this server
    just served.
    """
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOSTS", "wattracker.local")
    with TestClient(create_app()) as client:
        client.post(
            "/register", data={"username": "rider", "password": "password123"}
        )
        with client.websocket_connect(
            "/ride/ws?sim=1",
            headers={"Origin": "http://wattracker.local:8000",
                     "Host": "wattracker.local"},
        ) as ws:
            assert ws.receive_json()["status"] == "workout"


def test_the_ride_socket_still_refuses_a_foreign_origin(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("WATTRACKER_PUBLIC_HOSTS", "wattracker.local")
    with TestClient(create_app()) as client:
        client.post(
            "/register", data={"username": "rider", "password": "password123"}
        )
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/ride/ws?sim=1", headers={"Origin": "http://evil.example"}
            ) as ws:
                ws.receive_json()
        assert excinfo.value.code == 1008


# ----------------------------------------------------------------- cookie
def test_cookie_is_not_secure_by_default(monkeypatch):
    monkeypatch.delenv("WATTRACKER_COOKIE_SECURE", raising=False)
    assert config.cookie_secure() is False
    with TestClient(create_app()) as client:
        response = client.post(
            "/register", data={"username": "rider", "password": "password123"},
            follow_redirects=False,
        )
        assert "secure" not in response.headers.get("set-cookie", "").lower()


def test_cookie_can_be_marked_secure(monkeypatch):
    monkeypatch.setenv("WATTRACKER_COOKIE_SECURE", "1")
    assert config.cookie_secure() is True
    with TestClient(create_app(), base_url="https://testserver") as client:
        response = client.post(
            "/register", data={"username": "rider", "password": "password123"},
            follow_redirects=False,
        )
        assert "secure" in response.headers.get("set-cookie", "").lower()
