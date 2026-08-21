"""The tray window's login: a device token traded for a one-minute ticket.

What is being protected here is an escalation. A device token already lets a
connector read and write the rider's Zwift folders and upload rides as them;
these two routes additionally turn it into a browser session, which can read
the rider's whole history, change settings and take backups. That is a
deliberate widening (see docs/windows-security.md), and it is only defensible
if the ticket in the middle is genuinely single-use, genuinely short-lived, and
genuinely absent from the logs.

It is also only defensible if the escalated session cannot outlive the device
it came from. The pre-merge review of PR #93 walked the attack it otherwise
allows: device token -> ticket -> session -> ``POST /settings/connector`` ->
a *second* device with a fresh token, chosen label and all. Revoking the stolen
laptop then killed the original token and left the thief's one working, which
is the exact opposite of what the Settings page's Revoke button promises. The
last section of this module pins the answer to that.
"""
import base64
import contextlib
import inspect
import json
import logging

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import calendarfeed, connectorauth, connectorsession, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402

PASSWORD = "password123"


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": PASSWORD})
    return db.get_user_by_username(username)["id"]


def _paired(client, username="rider", label="Zwift PC"):
    uid = _register(client, username)
    _device_id, token = connectorauth.generate_token(uid, label)
    return uid, token


def _mint(client, token):
    return client.post(
        "/api/connector/session", headers={"Authorization": f"Bearer {token}"}
    )


@contextlib.contextmanager
def _connector_window(client, token):
    """The escalation, walked for real: device token -> ticket -> session.

    A brand-new client with no cookie of its own, exactly like the window
    pywebview opens - and exactly like an attacker holding nothing but a token
    off a retired laptop.
    """
    minted = _mint(client, token)
    assert minted.status_code == 200
    ticket = minted.json()["ticket"]
    with TestClient(client.app) as window:
        landing = window.get(
            f"/connector/session?token={ticket}", follow_redirects=False
        )
        assert landing.status_code == 303, "the escalation itself must still work"
        yield window


# ------------------------------------------------------------- the store
def test_a_ticket_is_redeemable_exactly_once():
    store = connectorsession.TicketStore()
    ticket = store.mint(7, "rider", 3)

    first = store.redeem(ticket)
    assert first == {"user_id": 7, "username": "rider", "device_id": 3}
    assert store.redeem(ticket) is None


def test_a_ticket_expires():
    now = {"t": 1000.0}
    store = connectorsession.TicketStore(ttl=60.0, clock=lambda: now["t"])
    ticket = store.mint(7, "rider", 3)

    now["t"] += 59.0
    assert store.redeem(ticket) is not None, "still inside the window"

    other = store.mint(7, "rider", 3)
    now["t"] += 61.0
    assert store.redeem(other) is None, "past the window"


def test_minting_again_invalidates_the_previous_ticket():
    """A connector that asks twice must not leave a spare credential behind."""
    store = connectorsession.TicketStore()
    first = store.mint(7, "rider", 3)
    second = store.mint(7, "rider", 3)

    assert store.redeem(first) is None
    assert store.redeem(second) is not None


def test_two_devices_do_not_invalidate_each_other():
    store = connectorsession.TicketStore()
    one = store.mint(7, "rider", 3)
    two = store.mint(7, "rider", 4)

    assert store.redeem(one) is not None
    assert store.redeem(two) is not None


@pytest.mark.parametrize(
    "bad", [None, 42, "", "short", "has spaces", "!" * 40, "A" * 500]
)
def test_a_malformed_ticket_is_refused_without_a_lookup(bad):
    store = connectorsession.TicketStore()
    store.mint(7, "rider", 3)
    assert store.redeem(bad) is None


def test_revoking_a_device_drops_its_ticket():
    store = connectorsession.TicketStore()
    ticket = store.mint(7, "rider", 3)
    store.revoke_device(3)
    assert store.redeem(ticket) is None


def test_unspent_tickets_do_not_accumulate():
    """A double-click whose window never opened must not leak an entry."""
    now = {"t": 0.0}
    store = connectorsession.TicketStore(ttl=60.0, clock=lambda: now["t"])
    for device_id in range(20):
        store.mint(7, "rider", device_id)
    assert store.outstanding == 20

    now["t"] += 61.0
    store.mint(7, "rider", 999)
    assert store.outstanding == 1


def test_the_ticket_itself_is_never_stored():
    store = connectorsession.TicketStore()
    ticket = store.mint(7, "rider", 3)
    assert ticket not in repr(store._tickets)
    assert connectorsession.hash_ticket(ticket) in repr(store._tickets)


# ------------------------------------------------------------- the routes
def test_minting_requires_a_real_device_token(client):
    _register(client)
    assert _mint(client, "A" * 43).status_code == 401
    assert client.post("/api/connector/session").status_code == 401


def test_a_minted_ticket_opens_a_real_session(client):
    """The whole point: after redeeming, ordinary pages load."""
    _uid, token = _paired(client)

    minted = _mint(client, token)
    assert minted.status_code == 200
    ticket = minted.json()["ticket"]
    assert minted.json()["expires_in"] == connectorsession.TICKET_TTL_S

    # A brand-new client, with no cookie of its own - the window pywebview
    # opens has never logged in.
    with TestClient(client.app) as window:
        landing = window.get(
            f"/connector/session?token={ticket}", follow_redirects=False
        )
        assert landing.status_code == 303
        assert landing.headers["location"] == "/"

        page = window.get("/settings", follow_redirects=False)
        assert page.status_code == 200


def test_a_redeemed_ticket_cannot_be_replayed(client):
    _uid, token = _paired(client)
    ticket = _mint(client, token).json()["ticket"]

    with TestClient(client.app) as window:
        assert window.get(
            f"/connector/session?token={ticket}", follow_redirects=False
        ).status_code == 303

    with TestClient(client.app) as attacker:
        attacker.get(f"/connector/session?token={ticket}", follow_redirects=False)
        # Refused, and - the part that matters - no session was created.
        assert attacker.get(
            "/settings", follow_redirects=False
        ).headers["location"] == "/login"


def test_a_bad_ticket_says_nothing_and_creates_nothing(client):
    _register(client)
    with TestClient(client.app) as stranger:
        response = stranger.get(
            "/connector/session?token=" + "A" * 43, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert stranger.get(
            "/settings", follow_redirects=False
        ).headers["location"] == "/login"


def test_a_ticket_carries_only_its_own_users_session(client):
    """Two riders, two connectors: a ticket must not cross between them."""
    _one, token_one = _paired(client, "one", "PC one")
    uid_two, _token_two = _paired(client, "two", "PC two")

    ticket = _mint(client, token_one).json()["ticket"]
    with TestClient(client.app) as window:
        window.get(f"/connector/session?token={ticket}", follow_redirects=False)
        # Logged in as "one", so "two"'s devices are not listed.
        page = window.get("/settings")
        assert "PC two" not in page.text
        assert "PC one" in page.text
    assert uid_two != _one


def test_a_revoked_device_cannot_mint(client):
    uid, token = _paired(client)
    device_id = connectorauth.list_devices(uid)[0]["id"]

    client.post(f"/settings/connector/{device_id}/revoke")

    assert _mint(client, token).status_code == 401


def test_revoking_kills_a_ticket_already_in_flight(client):
    """A minute-wide window, but "revoked" must mean revoked."""
    uid, token = _paired(client)
    ticket = _mint(client, token).json()["ticket"]
    device_id = connectorauth.list_devices(uid)[0]["id"]

    client.post(f"/settings/connector/{device_id}/revoke")

    with TestClient(client.app) as window:
        window.get(f"/connector/session?token={ticket}", follow_redirects=False)
        assert window.get(
            "/settings", follow_redirects=False
        ).headers["location"] == "/login"


def test_the_mint_response_is_not_cacheable(client):
    _uid, token = _paired(client)
    response = _mint(client, token)
    assert response.headers["cache-control"] == "private, no-store"


# ------------------------------------------------------------- the log
def test_the_ticket_is_scrubbed_from_the_access_log(client):
    """Why the query parameter is named "token" and must stay named that.

    uvicorn logs the full request target. calendarfeed's filter redacts
    parameters whose name starts with "token"; a ticket under any other name
    would be written to the access log in plaintext, which is precisely the
    thing hashing it in memory is meant to avoid.
    """
    _uid, token = _paired(client)
    ticket = _mint(client, token).json()["ticket"]

    # The redaction below only fires on a parameter named "token", so pin the
    # route's own parameter name rather than only the string built here.
    redeem = next(
        route for route in client.app.routes
        if getattr(route, "path", None) == "/connector/session"
    )
    assert "token" in inspect.signature(redeem.endpoint).parameters

    calendarfeed.install_access_log_redaction()
    access = logging.getLogger("uvicorn.access")
    record = access.makeRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/1.1" %d',
        ("127.0.0.1:1", "GET", f"/connector/session?token={ticket}", 303),
        None,
    )
    for log_filter in access.filters:
        log_filter.filter(record)

    assert ticket not in record.getMessage()
    assert "token=[REDACTED]" in record.getMessage()


# ------------------------------------------- what an escalated session may not do
def test_a_connector_session_cannot_pair_a_second_device(client):
    """The blocker the PR #93 review found, as an executable exploit.

    Pairing from inside a connector-opened window mints a token that survives
    revoking the device that opened it, so the one control the docs offer for a
    stolen laptop stops working - and the owner's only cue is an extra row in a
    list, under a label the attacker chose.
    """
    uid, token = _paired(client)
    before = [d["id"] for d in connectorauth.list_devices(uid)]

    with _connector_window(client, token) as window:
        response = window.post("/settings/connector", data={"label": "Spare"})

    assert response.status_code == 403
    # An explained refusal, not a bare status: the rider who hits this by
    # accident has to be told the way through it.
    assert "Sign in with your password first." in response.text
    # The load-bearing half: refusing must mean nothing was minted. A 403 with a
    # row in the table behind it would be worse than no check at all.
    assert [d["id"] for d in connectorauth.list_devices(uid)] == before


def test_a_connector_session_cannot_rotate_the_calendar_link(client):
    """Rotating points the rider's whole training calendar at a new URL.

    The attacker keeps the new link, the rider's calendar app quietly stops
    updating, and nothing about that reads as a compromise.
    """
    uid, token = _paired(client)
    live = calendarfeed.generate_token(uid)

    with _connector_window(client, token) as window:
        response = window.post("/settings/calendar-feed")

    assert response.status_code == 403
    # If the rotation had gone through, the rider's existing link would be dead.
    assert client.get(f"{calendarfeed.FEED_PATH}?token={live}").status_code == 200


def test_a_connector_session_cannot_replace_the_shared_anthropic_key(client):
    """The API key is app-global, not per-user (config.set_anthropic_api_key).

    A session that can swap it points every coaching request this server makes
    at an account the attacker owns, which hands them the prompt contents too.
    The rest of POST /settings still has to work - configuring folders is what
    the tray window is *for* - so only this one field is refused.
    """
    from wattracker import config

    uid, token = _paired(client)

    with _connector_window(client, token) as window:
        response = window.post(
            "/settings", data={"ftp": "250", "anthropic_api_key": "sk-ant-attacker"}
        )

    assert response.status_code == 200, "the rest of the page must still save"
    assert not config.anthropic_api_key_set()
    assert db.get_user_settings(uid)["ftp"] == 250


def test_a_connector_session_can_still_revoke(client):
    """Revoking is the way out, so it must never be the thing that is blocked.

    A rider who has lost the laptop may well be looking at the tray window of
    the machine still in front of them.
    """
    uid, token = _paired(client)
    _device_id, other = connectorauth.generate_token(uid, "Old laptop")
    doomed = next(d for d in connectorauth.list_devices(uid) if d["label"] == "Old laptop")

    with _connector_window(client, token) as window:
        response = window.post(f"/settings/connector/{doomed['id']}/revoke")

    assert response.status_code == 200
    assert connectorauth.device_for_token(other) is None


def test_a_password_login_pairs_and_rotates_as_before(client):
    """The negative control: the restriction must not reach an ordinary login."""
    uid, _token = _paired(client)
    client.post("/logout")
    assert client.post(
        "/login", data={"username": "rider", "password": PASSWORD}
    ).status_code == 200

    assert client.post("/settings/connector", data={"label": "New PC"}).status_code == 200
    assert len(connectorauth.list_devices(uid)) == 2
    assert client.post("/settings/calendar-feed").status_code == 200


def test_typing_the_password_in_the_tray_window_lifts_the_restriction(client):
    """A session is restricted by where it came from, not by which window it is in.

    The rider who actually signs in - inside the connector's own window, with no
    logout in between - is a rider who proved they know the password, which the
    review confirmed a device token cannot obtain or change. Anything less makes
    the connector window a permanently second-class UI.
    """
    uid, token = _paired(client)

    with _connector_window(client, token) as window:
        assert window.post(
            "/settings/connector", data={"label": "Spare"}
        ).status_code == 403

        assert window.post(
            "/login", data={"username": "rider", "password": PASSWORD}
        ).status_code == 200

        assert window.post(
            "/settings/connector", data={"label": "New PC"}
        ).status_code == 200
    assert len(connectorauth.list_devices(uid)) == 2


def test_the_connector_marker_cannot_be_stripped_by_its_holder(client):
    """The refusal rests on a signed cookie, not on anything the client owns.

    Starlette's SessionMiddleware base64s the session and signs it with
    ``config.session_secret()`` (256 bits, itsdangerous TimestampSigner). The
    payload is readable - so this test can assert the marker is really in there
    - but editing it invalidates the signature, and an unsigned cookie is not a
    session at all.
    """
    _uid, token = _paired(client)

    with _connector_window(client, token) as window:
        cookie = window.cookies["wattracker_session"]

    payload, _dot, signature = cookie.partition(".")
    session = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
    assert session["via"] == "connector"

    # The control for the control: carrying the cookie across clients by hand
    # has to work, or the refusal below would prove nothing about the signature.
    with TestClient(client.app) as carried:
        carried.cookies.set("wattracker_session", cookie)
        assert carried.get("/settings", follow_redirects=False).status_code == 200

    del session["via"]
    forged = base64.b64encode(json.dumps(session).encode()).decode() + "." + signature
    with TestClient(client.app) as attacker:
        attacker.cookies.set("wattracker_session", forged)
        # Not "refused to pair" - refused a session at all.
        assert attacker.get(
            "/settings", follow_redirects=False
        ).headers["location"] == "/login"


def test_revoking_the_device_leaves_no_working_credential(client):
    """What the Revoke button promises, walked end to end.

    Both halves matter. The token dying is the half that already worked; the
    window dying is the half that did not, because a session cookie is a signed
    blob with no server-side record and revocation has nothing to reach into.
    Without it a thief keeps reading the rider's history - and driving whichever
    connector is attached *now*, since RemoteBackend resolves by user_id alone -
    for the fortnight the cookie is valid.
    """
    uid, token = _paired(client)
    device_id = connectorauth.list_devices(uid)[0]["id"]

    with _connector_window(client, token) as window:
        assert window.get("/settings", follow_redirects=False).status_code == 200

        client.post(f"/settings/connector/{device_id}/revoke")

        assert _mint(client, token).status_code == 401
        assert window.get(
            "/settings", follow_redirects=False
        ).headers["location"] == "/login"
    assert connectorauth.list_devices(uid) == []
