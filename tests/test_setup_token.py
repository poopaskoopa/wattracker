"""Who gets the FIRST account, and why "whoever asks first" stopped being it.

The old policy was that an empty database accepts any registration, on the
reasoning that an install has to bootstrap and there is nothing to protect yet.
That is true on loopback and false the moment the app is bound past it - a
supported configuration, because it is how a phone becomes a ride screen. On a
fresh LAN-bound install the first person to reach ``/register`` owns the
instance, and an account here is not a private history: the LLM settings are
app-global, so the account that claims the install can point every rider's
prompts (and the stored API key) at a host it controls. Issue #132, item 4.

The fix is one secret with one job: a token generated at startup while the
database has no users, printed to the server's own output, and required by the
registration that claims the install. ``wattracker/setuptoken.py`` carries the
design argument, including why it is regenerated on every start rather than
persisted.

These tests are written as ATTACKS wherever there is an attack to run - no
token, wrong token, replayed token, two racing registrations, a flood aimed at
the shared hash limiter - because the happy path passing proves only that the
feature exists, not that it holds.

NOTE ON THE FIXTURES: tests/conftest.py relaxes the token check for the whole
suite (``bootstrap_setup_token``), because ~50 modules register a rider on
their way to testing something else and cannot know a per-instance token. This
module is where the check itself lives, so it takes ``enforce_setup_token`` for
every test and runs against the real implementation.
"""
import hmac
import re
import threading

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import auth, db, setuptoken  # noqa: E402
from wattracker.server import create_app  # noqa: E402

PASSWORD = "password123"


@pytest.fixture(autouse=True)
def _real_token_check(enforce_setup_token):
    """Every test in this module runs against the shipped check."""


@pytest.fixture()
def closed(monkeypatch):
    """The shipped default: no WATTRACKER_ALLOW_REGISTRATION at all.

    Same fixture, same reason, as tests/test_registration_policy.py: the
    suite-wide conftest sets that variable so unrelated tests can add a second
    rider, and the modules that own the policy delete it again.
    """
    monkeypatch.delenv("WATTRACKER_ALLOW_REGISTRATION", raising=False)


@pytest.fixture()
def client():
    with TestClient(create_app()) as value:
        yield value


def _token(client) -> str:
    """The token this app instance printed at startup."""
    return client.app.state.setup_token.value


def _register(client, username, token=None, password=PASSWORD):
    data = {"username": username, "password": password}
    if token is not None:
        data["setup_token"] = token
    return client.post("/register", data=data)


# --------------------------------------------------------- the token itself
def test_the_token_is_unguessable_and_never_repeats():
    """256 bits from the OS CSPRNG, not a timestamp or a counter.

    Asserted as a property rather than by reading the implementation: 500
    tokens, all distinct, all the 43-character base64url shape that
    ``secrets.token_urlsafe(32)`` produces. A generator seeded from the clock
    or reusing a value would fail one of the two.
    """
    tokens = [setuptoken.SetupToken().value for _ in range(500)]

    assert len(set(tokens)) == 500
    assert all(len(t) == 43 for t in tokens)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", t) for t in tokens)


def test_every_refusal_costs_exactly_one_constant_time_comparison(monkeypatch):
    """A wrong guess must not be distinguishable from a malformed one.

    ``==`` on a str short-circuits at the first differing character, which is
    the leak that lets a guesser walk a secret out one character at a time. The
    test counts calls into ``hmac.compare_digest`` rather than reading the
    source, so it keeps meaning something if the implementation is rewritten:
    every one of these paths - right, wrong, empty, absent, wrong type,
    absurdly long, spent - must run exactly one comparison and no more.
    """
    token = setuptoken.SetupToken()
    calls = []
    real = hmac.compare_digest

    def counting(left, right):
        calls.append((type(left), type(right)))
        return real(left, right)

    monkeypatch.setattr(setuptoken.hmac, "compare_digest", counting)

    assert token.matches(token.value) is True
    assert token.matches("wrong") is False
    assert token.matches("") is False
    assert token.matches(None) is False
    assert token.matches(12345) is False
    assert token.matches("x" * (setuptoken.MAX_SUBMITTED_LEN + 1)) is False
    # A prefix of the real token is the shape a character-at-a-time attack
    # produces, and it must be exactly as unremarkable as any other miss.
    assert token.matches(token.value[:-1]) is False
    token.spend()
    assert token.matches(token.value) is False

    assert len(calls) == 8


def test_a_spent_token_never_works_again():
    """One-time is a property of the token, not of its caller's ordering."""
    token = setuptoken.SetupToken()
    assert token.matches(token.value) is True

    token.spend()
    token.spend()  # idempotent

    assert token.spent is True
    assert token.matches(token.value) is False


def test_the_banner_prints_once_and_carries_the_token(capsys):
    """The operator's only copy, and only ever one of it.

    Once per instance because two callers reach it (startup, and the fallback
    in the registration refusal); a request-driven caller that could print
    repeatedly would be a way for an unauthenticated stranger to make the
    server shout.
    """
    token = setuptoken.SetupToken()

    assert token.announce() is True
    first = capsys.readouterr().out
    assert token.value in first
    assert "setup token" in first

    assert token.announce() is False
    assert capsys.readouterr().out == ""


def test_a_spent_token_is_never_printed(capsys):
    """Nothing that has already claimed an install goes back on the screen."""
    token = setuptoken.SetupToken()
    token.spend()

    assert token.announce() is False

    assert capsys.readouterr().out == ""


# ------------------------------------------------------------- the startup
def test_startup_prints_the_token_when_the_install_has_no_account(capsys):
    """The bootstrap case: no account, so the token is announced."""
    with TestClient(create_app()) as client:
        printed = capsys.readouterr().out
        assert _token(client) in printed
        assert client.app.state.setup_token.announced is True


def test_startup_says_nothing_once_an_account_exists(capsys):
    """"Irrelevant" has to mean invisible too.

    A server that already has an account must not keep printing a credential
    into ``~/.wattracker/server.log`` on every restart for the rest of its
    life - the token can do nothing at that point, and a secret-shaped string
    in a log invites someone to try it anyway.
    """
    db.init_db()
    db.create_user("rider", auth.hash_password(PASSWORD))

    with TestClient(create_app()) as client:
        printed = capsys.readouterr().out
        assert _token(client) not in printed
        assert "setup token" not in printed
        assert client.app.state.setup_token.announced is False


# ------------------------------------------------------- claiming the install
def test_registration_without_a_token_is_refused(closed, client):
    """The land grab itself: a stranger on the LAN reaching a fresh install.

    This is the whole vulnerability in one request. It used to return 200 and
    hand over the instance.
    """
    response = _register(client, "intruder")

    assert response.status_code == 403
    assert db.list_usernames() == []


def test_registration_with_a_wrong_token_is_refused(closed, client):
    """Guessing is the only remaining move, and it does not work."""
    response = _register(client, "intruder", token="not-the-token")

    assert response.status_code == 403
    assert db.list_usernames() == []


def test_a_near_miss_is_refused_like_any_other_guess(closed, client):
    """One character short of correct is simply wrong.

    Pinning this against a future "be helpful about typos" change: any
    tolerance here is a shortcut for a guesser, and the owner can paste again.
    """
    response = _register(client, "intruder", token=_token(client)[:-1])

    assert response.status_code == 403
    assert db.list_usernames() == []


def test_the_right_token_claims_the_install(closed, client):
    """The owner, who can see the server's output, gets in and is signed in."""
    response = _register(client, "rider", token=_token(client))

    assert response.status_code == 200
    assert db.get_user_by_username("rider") is not None
    # Registration still signs the new account in - the wizard follows it.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_the_token_is_spent_by_the_account_it_created(closed, client):
    """Single use, and not merely because the row count changed.

    The database is emptied again afterwards, so the ONLY thing standing
    between the replayed token and a second bootstrap account is
    ``SetupToken.spend``. Without it this returns 200: the policy check would
    see an empty database and step aside, exactly as it did before this
    feature existed.
    """
    assert _register(client, "rider", token=_token(client)).status_code == 200
    token = _token(client)
    conn = db.connect()
    try:
        conn.execute("DELETE FROM users")
        conn.commit()
    finally:
        conn.close()
    assert db.list_usernames() == []

    with TestClient(client.app) as attacker:
        replay = _register(attacker, "intruder", token=token)

    assert replay.status_code == 403
    assert db.list_usernames() == []


def test_a_rejected_password_does_not_burn_the_token(closed, client):
    """The owner mistyping their password must not cost them the install.

    Credential validation fails before anything is created, so there is
    nothing for the token to have paid for. Spending it there would leave the
    only recovery being a server restart.
    """
    short = _register(client, "rider", token=_token(client), password="short")
    assert short.status_code == 200  # the form, re-rendered with an error
    assert "at least 8" in short.text or "8 characters" in short.text
    assert db.list_usernames() == []

    assert _register(client, "rider", token=_token(client)).status_code == 200
    assert db.list_usernames() == ["rider"]


def test_the_opt_in_variable_does_not_open_the_bootstrap_account(
    client, monkeypatch
):
    """WATTRACKER_ALLOW_REGISTRATION governs ADDITIONAL accounts, and only those.

    The two controls answer different questions - "may there be another
    account?" and "who owns this install?" - so the second-rider opt-in must
    not be a way to hand the install to the network. A rider who set the
    variable in a service file months ago has not consented to that.
    """
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")

    assert _register(client, "intruder").status_code == 403
    assert db.list_usernames() == []

    # ...and with the token, the same opt-in behaves exactly as documented:
    # the first account takes the token, the second does not need one.
    assert _register(client, "rider", token=_token(client)).status_code == 200
    with TestClient(client.app) as second:
        assert _register(second, "bob").status_code == 200
    assert db.list_usernames() == ["bob", "rider"]


def test_an_unreadable_database_demands_the_token(client, monkeypatch):
    """"I cannot tell whether an account exists" must not mean "help yourself".

    Reachable only with the second-rider opt-in set, because otherwise the
    policy check refuses on the same uncertainty first. The startup banner is
    printed under the same failure (see the lifespan), so the token being
    demanded here is one the operator has actually been shown.
    """
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")
    token = _token(client)
    monkeypatch.setattr(
        db, "user_ids", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no"))
    )

    assert _register(client, "intruder").status_code == 403
    assert _register(client, "intruder", token="wrong").status_code == 403
    assert db.list_usernames() == []

    assert _register(client, "rider", token=token).status_code == 200
    assert db.list_usernames() == ["rider"]


# ------------------------------------------------------------ the race
def test_two_simultaneous_first_registrations_cannot_both_win(closed, client,
                                                              monkeypatch):
    """The TOCTOU the registration lock exists for, now with a token in it.

    Both requests carry the SAME valid token and are forced to overlap: the
    barrier inside the hash means neither can reach the lock until both have
    cleared the cheap pre-hash gate, which is precisely the interleaving where
    a naive "check then insert" admits two bootstrap accounts. Exactly one may
    come out with an account, and the token must be spent by the winner.
    """
    token = _token(client)
    barrier = threading.Barrier(2, timeout=30)
    real_hash = auth.hash_password

    def synchronized_hash(password):
        digest = real_hash(password)
        barrier.wait()
        return digest

    monkeypatch.setattr(auth, "hash_password", synchronized_hash)

    statuses = []
    guard = threading.Lock()

    def attempt(name):
        response = _register(client, name, token=token)
        with guard:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(name,))
               for name in ("rider", "intruder")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(statuses) == [200, 403]
    assert len(db.list_usernames()) == 1
    assert client.app.state.setup_token.spent is True


# ------------------------------------------- the refusal, and what it costs
def test_the_refusal_costs_no_password_hash(closed, client, monkeypatch):
    """Refuse before scrypt, or the token becomes a memory-burning gadget.

    Each hash reserves ~128 MiB from a limiter shared with /login. A token
    check placed after the hash would mean anyone who can reach a fresh
    install can exhaust that limiter for free and take /login down with it -
    which is the same failure the registration policy check is ordered ahead
    of hashing to avoid.
    """
    hashed = []
    real = auth.hash_password
    monkeypatch.setattr(
        auth, "hash_password", lambda pw: (hashed.append(pw), real(pw))[1]
    )

    assert _register(client, "intruder").status_code == 403
    assert _register(client, "intruder", token="wrong").status_code == 403

    assert hashed == []


def test_a_wrong_token_is_refused_ahead_of_the_hash_limiter(closed, monkeypatch):
    """Ordering, asserted where it is visible: token first, then capacity.

    With the only hash slot occupied, a request that would otherwise be SHED
    (503) is refused for the token instead (403). That is the proof the token
    gate runs before the reservation - if it ran after, this would be a 503 and
    every wrong token would be charging the limiter on its way to being wrong.
    """
    app = create_app()
    app.state.hash_limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=0, wait_timeout=0.0
    )
    with TestClient(app) as client:
        with app.state.hash_limiter.reserve():  # occupy the only slot
            refused = _register(client, "intruder", token="wrong")
            assert refused.status_code == 403
            # ...and the shed itself still works for a request that gets past
            # the token: this route's existing capacity behaviour is unchanged.
            shed = _register(client, "rider", token=_token(client))
            assert shed.status_code == 503
            assert shed.headers["retry-after"] == "5"
        assert app.state.hash_limiter.shed_total == 1
        assert db.list_usernames() == []

        # The slot is free again and the token was not consumed by any of it.
        assert _register(client, "rider", token=_token(client)).status_code == 200


def test_a_flood_of_wrong_tokens_cannot_lock_the_owner_out(closed, client):
    """The DoS the refusal deliberately does not defend against by locking.

    Refusing future attempts after wrong tokens would let anyone who can reach
    the port stop the owner completing setup - a cheaper version of the attack
    the token exists to stop. So 50 wrong guesses cost the attacker 50
    refusals, tell no throttle anything, and leave the owner able to walk
    straight in with the real token.
    """
    for index in range(50):
        assert _register(
            client, f"intruder{index}", token=f"guess{index}"
        ).status_code == 403

    # Nothing was recorded against /login's per-username throttle or its
    # unkeyed counter: a refused setup token is not a failed password.
    throttle = client.app.state.login_throttle
    assert all(throttle.retry_after(f"intruder{i}") == 0.0 for i in range(50))
    assert throttle.retry_after("rider") == 0.0
    assert client.app.state.login_failures.count == 0
    # Counted for visibility, refusing nothing.
    assert client.app.state.setup_token.refusals == 50

    assert _register(client, "rider", token=_token(client)).status_code == 200


def test_the_refusal_never_leaks_the_token(closed, client, capsys):
    """The page an attacker can see must not contain what they are guessing.

    Includes the console: the banner is printed once at startup, and a flood of
    refusals must not make the server print the token again where a shoulder or
    a shared log would pick it up.
    """
    capsys.readouterr()  # discard the startup banner
    token = _token(client)

    missing = _register(client, "intruder")
    wrong = _register(client, "intruder", token="wrong")

    assert token not in missing.text
    assert token not in wrong.text
    # Nothing about the guess, either - not its length, not how close it was.
    assert "wrong" not in wrong.text
    assert capsys.readouterr().out == ""


def test_the_refusal_is_a_page_that_says_where_the_token_comes_from(
    closed, client
):
    """The owner who pasted a stale token needs somewhere to try again.

    A bare 403 would be indistinguishable from "this install is broken", and
    the person who actually hits this is nearly always the owner, not an
    attacker.
    """
    response = _register(client, "rider", token="stale")

    assert response.status_code == 403
    assert "setup token" in response.text.lower()
    assert 'name="setup_token"' in response.text


# ------------------------------------------------------------- the form
def test_the_bootstrap_form_asks_for_the_token_and_does_not_show_it(
    closed, client
):
    response = client.get("/register")

    assert response.status_code == 200
    assert 'name="setup_token"' in response.text
    assert _token(client) not in response.text


def test_the_second_account_form_does_not_ask_for_a_token(client, monkeypatch):
    """A rider adding a second account has no token and must not be asked.

    The field is bootstrap-only; showing it to someone who cannot fill it is a
    dead end that reads as a broken page.
    """
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")
    assert _register(client, "rider", token=_token(client)).status_code == 200

    with TestClient(client.app) as second:
        response = second.get("/register")

    assert response.status_code == 200
    assert 'name="setup_token"' not in response.text
