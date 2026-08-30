"""The first run of a fresh install: /welcome, and what it must not change.

Before this, a brand-new install dropped the rider on /login - a form that
cannot possibly work, because the account it asks for does not exist yet - and
left them to notice a "Create one" link at the bottom. Three pages had to be
found in the right order (login, register, then the setup wizard) before the
app did anything. ``/welcome`` is the same journey with the account step folded
into the front of the existing wizard: create the account, get signed in by
that same request, and continue straight into weight/folder/FTP/ZwiftPower.

What is deliberately NOT here is any proof that the person at /welcome is the
person at the console. There is no setup token, no code to copy, no console
step. That was tried (PR #147) and rejected: it is a hurdle in front of a
layperson's first five minutes, and the exposure it guarded against is already
closed one layer down - WATTRACKER_HOST defaults to 127.0.0.1 and config.py
refuses a non-loopback bind unless WATTRACKER_ALLOW_NON_LOOPBACK is also set.
On a default install nobody but the person at the keyboard can reach this route
at all. The residual land-grab risk on a deliberately LAN-exposed install is
accepted by owner decision.

What IS here, and what the module is really guarding, is that a nicer front
door did not become a second, weaker one: /welcome hashes a password while
unauthenticated, exactly like /login and /register, so it must observe the same
origin check, the same global hash ceiling ahead of any hashing, and the same
lock around the "is this really the first account" decision.
"""
import threading

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import auth, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402

PASSWORD = "password123"


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def _welcome(client, username="rider", password=PASSWORD, **kw):
    return client.post(
        "/welcome", data={"username": username, "password": password}, **kw
    )


# ------------------------------------------------- landing on a fresh install
def test_a_fresh_install_sends_a_visitor_to_the_wizard_not_to_login(client):
    """The whole point: opening the app on a new install starts setup."""
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/welcome"


def test_the_wizard_asks_for_an_account_first(client):
    page = client.get("/welcome")

    assert page.status_code == 200
    assert 'name="username"' in page.text
    assert 'name="password"' in page.text
    # Numbered as one journey, not as a page that happens to precede another.
    assert "Step 1 of 5" in page.text


def test_login_still_renders_but_says_where_to_start(client):
    """/login keeps answering 200 - the trusted-host checks, the tray window
    and bookmarks all reach it - but it no longer pretends to be usable."""
    page = client.get("/login")

    assert page.status_code == 200
    assert "/welcome" in page.text
    assert "has not been set up yet" in page.text


# ------------------------------------------ creating the account and going on
def test_creating_the_account_signs_the_rider_in_and_continues(client):
    """No second login. The redirect lands in the wizard already signed in."""
    response = _welcome(client, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert db.get_user_by_username("rider") is not None

    followed = client.get("/setup")
    assert followed.status_code == 200
    # Signed in: the nav only renders for a session that has a username.
    assert "Logout" in followed.text


def test_the_wizard_keeps_counting_from_the_account_step(client):
    """One continuous first run, so the numbering does not restart at 1.

    The account was step 1, so the remaining steps are 2..5 of 5. A rider who
    made a SECOND account at /register never saw that step and gets 1..4 of 4
    (the next test), which is why this is a session flag rather than something
    derived from the account itself.
    """
    _welcome(client)

    page = client.get("/setup").text
    assert 'data-step-offset="1"' in page
    assert "<span>2</span> Body weight" in page
    assert "<span>5</span> ZwiftPower" in page


def test_a_later_account_gets_the_plain_four_step_wizard(client, monkeypatch):
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")
    _welcome(client, "rider")

    with TestClient(client.app) as second:
        second.post("/register", data={"username": "bob", "password": PASSWORD})
        page = second.get("/setup").text

    assert 'data-step-offset="0"' in page
    assert "<span>1</span> Body weight" in page


def test_finishing_setup_stops_the_first_run_numbering(client, home_dir):
    """The offset is about the first run, so it ends when the first run does."""
    _welcome(client)
    uid = db.get_user_by_username("rider")["id"]
    activities = home_dir / "Activities"
    activities.mkdir()

    done = client.post("/setup/complete", data={
        "weight_kg": "72", "ftp_choice": "manual", "manual_ftp": "230",
        "zwiftpower": "no", "activities_dir": str(activities),
    })

    assert done.status_code == 200
    assert db.onboarding_complete(uid) is True
    # Back to /setup now redirects to Settings; the flag is gone either way.
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/settings"


# ------------------------------------------- once an account exists, nothing
def test_the_wizard_stops_intercepting_once_an_account_exists(client):
    _welcome(client, "rider")
    client.post("/logout")

    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/welcome", follow_redirects=False).headers["location"] == "/login"
    assert "has not been set up yet" not in client.get("/login").text


def test_the_wizard_cannot_create_a_second_account(client):
    """/welcome asks the narrow question - "is there NO account?" - rather than
    _registration_open, so the second-account policy stays entirely at
    /register where the page explains it. Even with the opt-in variable set,
    /welcome creates nothing: it is the first-run route, not a sign-up route.
    """
    _welcome(client, "rider")

    with TestClient(client.app) as intruder:
        response = _welcome(intruder, "squatter")

    assert response.status_code == 409
    assert db.get_user_by_username("squatter") is None
    assert db.list_usernames() == ["rider"]


def test_a_signed_in_rider_is_not_offered_the_wizard(client):
    _welcome(client, "rider")

    assert client.get("/welcome", follow_redirects=False).headers["location"] == "/"


# ------------------------------------------------------------- the guards
def test_the_wizard_rejects_a_cross_origin_post(client):
    """A drive-by page must not be able to claim a fresh install remotely.

    Same refusal as /login and /register, before anything is spent on it.
    """
    response = _welcome(client, headers={"origin": "http://evil.example"})

    assert response.status_code == 403
    assert db.get_user_by_username("rider") is None


def test_the_wizard_is_gated_by_the_same_hash_ceiling(client):
    """Unauthenticated scrypt is ~128 MiB a time and the ceiling is global.

    Adding a third route that hashes without reserving would just move a flood
    one route across, and would let it starve /login - the route the owner
    needs in order to get back in.
    """
    app = client.app
    app.state.hash_limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=0, wait_timeout=0.0
    )
    with app.state.hash_limiter.reserve():  # occupy the only slot
        shed = _welcome(client)

    assert shed.status_code == 503
    assert shed.headers["retry-after"] == "5"
    assert db.get_user_by_username("rider") is None
    # Slot free again: the first run works normally.
    assert _welcome(client, follow_redirects=False).status_code == 303


def test_shedding_happens_before_the_hash_and_records_no_failure(client, monkeypatch):
    """Two invariants in one, because breaking either is the same outage.

    If the shed ran AFTER hashing it would not be a shed at all. And if it
    recorded a login failure the flood would drive the owner's own username
    into lockout - the throttle is per-username, and a shed request never
    proved anything about a username.
    """
    app = client.app
    app.state.hash_limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=0, wait_timeout=0.0
    )
    monkeypatch.setattr(auth, "hash_password", _never_hash)

    with app.state.hash_limiter.reserve():
        assert _welcome(client).status_code == 503

    assert app.state.login_failures.count == 0
    assert app.state.login_throttle.retry_after("rider") == 0.0


def _never_hash(password):
    raise AssertionError("no password may be hashed once the limiter is full")


def test_a_bad_password_costs_no_hash_at_all(client, monkeypatch):
    """Validation is ahead of scrypt, so a too-short password is free."""
    monkeypatch.setattr(auth, "hash_password", _never_hash)

    response = _welcome(client, password="short")

    assert response.status_code == 400
    assert db.user_ids() == []


def test_two_simultaneous_first_runs_produce_exactly_one_account(monkeypatch):
    """Two riders claim the same fresh install at once; one account results.

    The barrier is what makes the overlap real rather than accidental: holding
    both threads INSIDE hash_password until the other arrives guarantees that
    both passed the cheap "no account yet" gate before either reached the
    insert. That is precisely the window the registration lock closes, and the
    reason the gate is re-asked under it rather than trusted from before the
    hash. Two threads rather than more because the hash limiter admits
    MAX_CONCURRENT_HASHES at a time; a third would be shed at the door and
    never reach the barrier.

    Honest about what it can and cannot prove: with the lock swapped for a
    no-op this test still passes, because what remains between the re-check and
    the INSERT is a couple of adjacent statements and CPython does not tend to
    switch threads there. So read it as a guard on the OUTCOME - exactly one
    account, the loser told so rather than silently ignored - and not as proof
    that the lock is load-bearing. The argument for the lock is in the source;
    this is the assertion that the outcome it exists for actually holds.
    """
    app = create_app()
    barrier = threading.Barrier(2, timeout=30)
    real_hash = auth.hash_password

    def synchronized(password):
        barrier.wait()
        return real_hash(password)

    monkeypatch.setattr(auth, "hash_password", synchronized)
    results = []
    lock = threading.Lock()
    # Both clients are opened here, in the main thread, and each keeps its own
    # cookie jar so neither request carries the other's brand-new session.
    # Opening them inside the worker threads instead means two TestClient
    # lifespans starting at once, which is a race in the harness rather than in
    # the app - it deadlocked, and the barrier below turned that into a
    # 30-second timeout that looked like a product bug.
    with TestClient(app) as first, TestClient(app) as second:
        def attempt(client, name):
            response = _welcome(client, name, follow_redirects=False)
            with lock:
                results.append(response.status_code)

        threads = [
            threading.Thread(target=attempt, args=(first, "rider0")),
            threading.Thread(target=attempt, args=(second, "rider1")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

    assert len(results) == 2, "a thread did not finish"
    assert results.count(303) == 1
    assert set(results) <= {303, 409}
    assert len(db.list_usernames()) == 1
