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
    assert uid_two != _one  # two accounts, which is the premise of the test

    ticket = _mint(client, token_one).json()["ticket"]
    with TestClient(client.app) as window:
        window.get(f"/connector/session?token={ticket}", follow_redirects=False)
        # Logged in as "one", so "two"'s devices are not listed.
        page = window.get("/settings")
        assert "PC two" not in page.text
        assert "PC one" in page.text


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

    The status is 403, matching the other two refusals. It was 200 for one
    commit, because the route set a message and then fell through to the
    ordinary "Settings saved." render; the key was never written, but the page
    said both things at once and the status told a script the write had gone
    through. The fields that are allowed are still written on the way past,
    which is what the ftp assertion below holds onto.
    """
    from wattracker import config

    uid, token = _paired(client)

    with _connector_window(client, token) as window:
        response = window.post(
            "/settings", data={"ftp": "250", "anthropic_api_key": "sk-ant-attacker"}
        )

    assert response.status_code == 403
    assert not config.anthropic_api_key_set()
    assert db.get_user_settings(uid)["ftp"] == 250, "the rest of the page must still save"


def test_a_connector_session_cannot_repoint_the_llm_endpoint(client):
    """The endpoint is the larger half of the same threat as the key.

    Once the provider is settable, a connector session that points it at a
    base URL the attacker controls is handed the shared key on the first
    refinement call and every rider's prompt payload after that - without ever
    posting a key, and without anything looking broken. Revoking the device
    does not undo it, which is the test the whole refusal list is built on.
    """
    from wattracker import config

    uid, token = _paired(client)
    config.set_llm_settings(endpoint="anthropic", model="m", api_key="rider-key")

    with _connector_window(client, token) as window:
        response = window.post(
            "/settings",
            data={"ftp": "250", "llm_endpoint": "custom",
                  "llm_custom_url": "http://attacker.example/v1"},
        )

    assert response.status_code == 403
    assert "Sign in with your password first." in response.text
    assert "Settings saved." not in response.text
    # Nothing about the group moved, key included.
    assert config.load_config().llm_endpoint == "anthropic"
    assert config.load_config().api_key == "rider-key"
    assert db.get_user_settings(uid)["ftp"] == 250, "the rest of the page must still save"


def test_a_connector_session_cannot_clear_the_stored_llm_model(client):
    """Clearing the model is a change to the group, so it is refused too.

    Blanking the model field is the form's way of saying "use the provider
    default", which for a custom endpoint disables the LLM layer outright. It
    writes app-level state either way, so it belongs inside the refusal rather
    than beside it.
    """
    from wattracker import config

    _uid, token = _paired(client)
    config.set_llm_settings(endpoint="openrouter", model="pinned", api_key="k")

    with _connector_window(client, token) as window:
        response = window.post(
            "/settings", data={"llm_endpoint": "openrouter", "llm_model": ""},
        )

    assert response.status_code == 403
    assert config.load_config().llm_model == "pinned"


def test_a_connector_session_still_saves_folders_with_the_llm_form_echoed(client):
    """The refusal is about changing the group, not about posting it.

    The LLM fields live in the same form as the folders, so the tray window
    sends the provider and model the page just rendered back with every save.
    If that echo counted as an attempt, the folder save this window exists for
    would 403 every time while preventing nothing - so an unchanged group is
    an ordinary save, and still writes none of the app-level LLM settings.
    """
    from wattracker import config

    uid, token = _paired(client)
    config.set_llm_settings(endpoint="openrouter", model="pinned", api_key="k")

    with _connector_window(client, token) as window:
        response = window.post(
            "/settings",
            data={"ftp": "250", "llm_endpoint": "openrouter",
                  "llm_model": "pinned", "api_key": ""},
        )

    assert response.status_code == 200
    assert "Settings saved." in response.text
    assert "Sign in with your password first." not in response.text
    assert db.get_user_settings(uid)["ftp"] == 250
    assert config.load_config().llm_model == "pinned"


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


def test_a_revoked_device_cannot_keep_riding_over_the_websocket(client):
    """The hole the independent re-check of the first fix found.

    AuthMiddleware is a BaseHTTPMiddleware, and Starlette hands any scope that
    is not "http" straight past it - so terminating the session there closed
    the browser half and left the ride socket wide open. A revoked laptop could
    still open /ride/ws on the same cookie and drive whichever connector is
    attached now, because _ble_session resolves by user_id alone. That is
    precisely the harm revoking is supposed to stop, reached through a
    different door.

    Both halves are asserted: the HTTP half must still terminate, and the
    socket must refuse. Asserting only the socket would pass against a build
    that had broken the middleware instead of fixing the socket.
    """
    uid, token = _paired(client)
    device_id = connectorauth.list_devices(uid)[0]["id"]

    with _connector_window(client, token) as window:
        # Paired, the window rides: without this the test would also pass if
        # the socket refused everyone.
        with window.websocket_connect(
            "/ride/ws?sim=1&type=endurance&minutes=30"
        ) as ws:
            assert ws.receive_json()["status"] != "error"

        client.post(f"/settings/connector/{device_id}/revoke")

        # The socket goes FIRST, and the order is the whole test. AuthMiddleware
        # clears the cookie when it terminates a revoked session, so an HTTP
        # request here would empty the session and leave the socket with no
        # user_id at all - which refuses the connection for the wrong reason and
        # passes just as happily against the bypass. Asked while the cookie is
        # still intact, this fails unless the socket checks for itself.
        with window.websocket_connect(
            "/ride/ws?sim=1&type=endurance&minutes=30"
        ) as ws:
            assert ws.receive_json() == {
                "status": "error", "error": "not authenticated"
            }

        # And only then the HTTP half, which must still terminate.
        assert window.get(
            "/settings", follow_redirects=False
        ).headers["location"] == "/login"


def test_a_password_session_still_rides(client):
    """The other side of the check above: it must cost an ordinary rider nothing.

    A password session carries no device_id, so _connector_session_still_paired
    returns True without a lookup. If this ever fails, the socket has started
    charging every rider for a check that exists for one case.
    """
    _register(client)
    client.post("/login", data={"username": "rider", "password": PASSWORD})

    with client.websocket_connect("/ride/ws?sim=1&type=endurance&minutes=30") as ws:
        assert ws.receive_json()["status"] != "error"


def test_the_refused_api_key_says_only_that_it_refused(client):
    """The refusal must not also claim to have saved.

    The key was never written - that half always held - but the route fell
    through to the ordinary render, so the page came back 200 with "Settings
    saved." above the refusal. Two contradictory statements about one request,
    and a status code that tells a script the write went through.
    """
    uid, token = _paired(client)

    with _connector_window(client, token) as window:
        response = window.post(
            "/settings", data={"anthropic_api_key": "sk-attacker"}
        )

    assert response.status_code == 403
    assert "Sign in with your password first." in response.text
    assert "Settings saved." not in response.text
