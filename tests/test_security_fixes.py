"""Tests for the audit security fixes: file modes, upload/FIT caps, directory
confinement, authenticated credential storage, WS origin, trusted hosts, and
disabled docs."""
import base64
import os
import secrets
import stat
import threading
import time
import types

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from wattracker import auth, backup, config, credstore, db, paths  # noqa: E402
from wattracker.ingest import fit_parser  # noqa: E402
from wattracker.prescribe import zwo  # noqa: E402
from wattracker import server as server_mod  # noqa: E402
from wattracker.server import create_app  # noqa: E402
from conftest import redirect_home  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="tester", password="password123"):
    return client.post("/register", data={"username": username, "password": password})


# ------------------------------------------------------- H1 file modes
@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_data_dir_db_config_and_backups_are_owner_only():
    d = config.app_data_dir()
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700

    config.set_anthropic_api_key("secret-key")  # writes config.json
    cfg = config.config_path()
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600

    db.init_db()
    assert stat.S_IMODE(os.stat(config.db_path()).st_mode) == 0o600

    bpath = backup.create_backup("manual")
    assert stat.S_IMODE(os.stat(bpath).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(backup.backups_dir()).st_mode) == 0o700


# --------------------------------------------------- M2a upload size cap
def test_upload_rejects_oversized_file(client, monkeypatch):
    _register(client)
    import wattracker.server as srv

    monkeypatch.setattr(srv, "MAX_UPLOAD_BYTES", 16)
    r = client.post(
        "/activities/upload",
        files={"file": ("big.fit", b"x" * 1024, "application/octet-stream")},
    )
    assert r.status_code == 413


# --------------------------------------------------- M2b FIT record cap
def test_parse_fit_caps_record_count(monkeypatch):
    class FakeMsg:
        name = "record"

        def has_field(self, f):
            return False

        def get_value(self, n):
            return None

    class FakeReader:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for _ in range(50):
                yield FakeMsg()

    fake = types.SimpleNamespace(FitReader=FakeReader, FitDataMessage=FakeMsg)
    monkeypatch.setattr(fit_parser, "fitdecode", fake)
    monkeypatch.setattr(fit_parser, "MAX_FIT_RECORDS", 10)
    with pytest.raises(ValueError):
        fit_parser.parse_fit("dummy.fit")


# --------------------------------------------------- M3 directory confinement
@pytest.mark.skipif(os.name == "nt", reason="/etc is POSIX-specific")
def test_settings_rejects_dir_outside_home(client):
    _register(client)
    r = client.post("/settings", data={"activities_dir": "/etc"})
    assert r.status_code == 200
    assert "inside your home directory" in r.text
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None


def test_settings_rejects_nonexistent_dir(client):
    _register(client)
    r = client.post(
        "/settings", data={"activities_dir": "/nonexistent/path/xyz123"}
    )
    assert "not found or not a directory" in r.text
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None


def test_settings_accepts_dir_under_home(client, tmp_path, monkeypatch):
    # setenv("HOME") alone is inert on Windows - ntpath.expanduser reads
    # USERPROFILE - so the sandboxed HOME stayed at tmp_path/home and this
    # test's sibling folder was (correctly) refused.
    redirect_home(monkeypatch, tmp_path)
    _register(client)
    good = tmp_path / "acts"
    good.mkdir()
    r = client.post("/settings", data={"activities_dir": str(good)})
    assert r.status_code == 200
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] == str(good)


def test_settings_accepts_configured_redirect_outside_home(
    client, tmp_path, monkeypatch
):
    home = tmp_path / "home"  # already the sandboxed HOME (conftest)
    redirected = tmp_path / "redirected-documents" / "Zwift" / "Activities"
    home.mkdir(exist_ok=True)
    redirected.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WATTRACKER_ACTIVITIES_DIR", str(redirected))
    _register(client)
    response = client.post(
        "/settings", data={"activities_dir": str(redirected)}
    )
    assert response.status_code == 200
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] == str(redirected)


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics tested on POSIX")
def test_settings_rejects_symlink_escape_from_trusted_root(
    client, tmp_path, monkeypatch
):
    home = tmp_path / "home"  # already the sandboxed HOME (conftest)
    outside = tmp_path / "outside"
    home.mkdir(exist_ok=True)
    outside.mkdir()
    link = home / "escaped"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    _register(client)
    response = client.post("/settings", data={"activities_dir": str(link)})
    assert "inside your home directory" in response.text
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None


# --------------------------------------------- L2 authenticated credstore
def test_credstore_new_format_roundtrip_and_tamper_detection(user_id):
    token = credstore._encrypt("s3cret-password")
    assert token.startswith("enc2$")
    assert credstore._decrypt(token) == "s3cret-password"

    # Flipping any ciphertext byte must fail the HMAC and refuse to decrypt.
    raw = bytearray(base64.b64decode(token[len("enc2$"):]))
    raw[18] ^= 0x01  # inside the ciphertext region (after the 16-byte nonce)
    tampered = "enc2$" + base64.b64encode(bytes(raw)).decode("ascii")
    assert credstore._decrypt(tampered) is None

    # Tampering the appended tag also fails.
    raw2 = bytearray(base64.b64decode(token[len("enc2$"):]))
    raw2[-1] ^= 0x01
    tampered2 = "enc2$" + base64.b64encode(bytes(raw2)).decode("ascii")
    assert credstore._decrypt(tampered2) is None


def test_credstore_legacy_blob_still_decrypts(user_id):
    # An old unauthenticated enc1$ blob written with the raw key keystream.
    key = credstore._install_key()
    nonce = secrets.token_bytes(16)
    data = b"legacy-pw"
    cipher = bytes(
        a ^ b for a, b in zip(data, credstore._keystream(key, nonce, len(data)))
    )
    token = "enc1$" + base64.b64encode(nonce + cipher).decode("ascii")
    assert credstore._decrypt(token) == "legacy-pw"


# ------------------------------------------------------ L3 WS origin allowlist
def test_ws_rejects_cross_origin(client):
    _register(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ride/ws?sim=1", headers={"origin": "http://evil.example.com"}
        ) as ws:
            ws.receive_json()


def test_ws_allows_local_origin(client):
    _register(client)
    with client.websocket_connect(
        "/ride/ws?sim=1&type=endurance&minutes=30",
        headers={"origin": "http://localhost:8000"},
    ) as ws:
        msg = ws.receive_json()
        assert "status" in msg


# ------------------------------------------------------ L4 trusted hosts
def test_untrusted_host_rejected(client):
    r = client.get("/login", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_trusted_host_accepted(client):
    assert client.get("/login").status_code == 200  # default host "testserver"


@pytest.mark.parametrize("host", ["[::1]", "[::1]:8000", "::1"])
def test_ipv6_loopback_trusted_host_accepted(client, host):
    assert client.get("/login", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["[::2]", "[2001:db8::1]:8000", "localhost:bad"])
def test_other_ipv6_and_malformed_hosts_rejected(client, host):
    assert client.get("/login", headers={"host": host}).status_code == 400


# ------------------------------------------------------ L5 docs disabled
def test_interactive_docs_disabled(client):
    _register(client)  # authenticate so we see 404, not the auth redirect
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ------------------------------------------------------ L1 logout is POST-only
def test_logout_get_not_allowed(client):
    _register(client)
    # GET /logout no longer exists; it must not clear the session.
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (404, 405)
    assert client.get("/", follow_redirects=False).status_code == 200  # still authed


# =====================================================================
# 2026-07-27 review
# =====================================================================

def _poison_setting(user_id: int, key: str, value: str) -> None:
    """Write a settings value straight into the DB, past every write-side check.

    This is the "already stored before the fix" case: a row written by an older
    build (POST /activities/rescan persisted whatever was posted), restored from
    a backup, or hand-edited. Nothing may treat it as trusted merely because it
    is in the database now.
    """
    db.save_user_settings(user_id, {})  # ensure the row exists
    conn = db.connect()
    try:
        conn.execute(
            f"UPDATE user_settings SET {key} = ? WHERE user_id = ?", (value, user_id)
        )
        conn.commit()
    finally:
        conn.close()
    assert db.get_user_settings(user_id)[key] == value


# ------------------- H1 (review) /login password-hash memory exhaustion
#
# scrypt at the configured cost peaks at ~128 MiB per hash and /login is
# unauthenticated, so what has to be bounded is the number of hashes running at
# once. LoginThrottle cannot do it: it is keyed by username, and the attack
# rotates the username on every request.

def test_hash_limiter_caps_concurrency_and_sheds_the_excess():
    limiter = auth.PasswordHashLimiter(
        max_concurrent=2, max_waiting=0, wait_timeout=0.0
    )
    release = threading.Event()
    holding = threading.Semaphore(0)
    shed = []

    def worker():
        try:
            with limiter.reserve():
                holding.release()
                release.wait(timeout=10)
        except auth.HashCapacityExceeded:
            shed.append(1)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        # Exactly max_concurrent hashes may be in flight, whatever the order the
        # threads got there in; the other four are refused, not queued.
        assert holding.acquire(timeout=10)
        assert holding.acquire(timeout=10)
        deadline = time.time() + 10
        while len(shed) < 4 and time.time() < deadline:
            time.sleep(0.01)
        assert len(shed) == 4  # uncapped, none of them would ever be refused
        assert limiter.in_flight == 2
    finally:
        release.set()
        for t in threads:
            t.join(timeout=10)
    assert limiter.peak_in_flight == 2
    assert limiter.shed_total == 4
    assert len(shed) == 4
    assert limiter.in_flight == 0


def test_hash_limiter_queues_briefly_then_sheds_on_timeout():
    """A short bounded wait, then a shed: queueing without a bound just turns
    memory exhaustion into unbounded latency (and pins ASGI worker threads)."""
    limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=4, wait_timeout=0.05
    )
    release = threading.Event()
    with limiter.reserve():
        # A waiter that never gets a slot is shed once its timeout expires.
        with pytest.raises(auth.HashCapacityExceeded):
            with limiter.reserve():
                pass
    release.set()
    # With the slot free again, the next caller is served normally.
    with limiter.reserve():
        assert limiter.in_flight == 1


def test_login_flood_with_rotating_usernames_cannot_exceed_the_ceiling(monkeypatch):
    """The actual attack: cross-origin-style POSTs that never repeat a username.

    Without a global cap every one of them buys ~128 MiB concurrently. The
    assertion is on the cap being enforced (peak in-flight hashes and shed
    responses), not on RSS.
    """
    app = create_app()
    limiter = auth.PasswordHashLimiter(
        max_concurrent=2, max_waiting=2, wait_timeout=0.05
    )
    app.state.hash_limiter = limiter
    attempts = 12
    with TestClient(app) as c:
        c.post("/register", data={"username": "owner", "password": "password123"})
        c.post("/logout")

        # Stand in for scrypt: same shape, same cost profile, no 128 MiB.
        def slow_verify(password, stored):
            time.sleep(0.25)
            return False

        monkeypatch.setattr(auth, "verify_password", slow_verify)
        monkeypatch.setattr(auth, "dummy_verify", lambda password: time.sleep(0.25))

        statuses = []
        lock = threading.Lock()

        def attempt(i):
            r = c.post(
                "/login",
                data={"username": f"ghost{i}", "password": "password123"},
            )
            with lock:
                statuses.append(r.status_code)

        threads = [threading.Thread(target=attempt, args=(i,))
                   for i in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert len(statuses) == attempts
    # THE ceiling: the cap binds (the flood did overlap) and is never exceeded.
    assert limiter.peak_in_flight == limiter.max_concurrent == 2
    # The excess is shed fast, not queued behind an unbounded backlog.
    assert limiter.shed_total >= 1
    assert statuses.count(503) == limiter.shed_total
    assert statuses.count(200) >= 1  # genuine attempts still got answered
    assert set(statuses) <= {200, 503}
    # The per-username throttle is blind to this shape - which is why the
    # global cap has to exist. Not one of these usernames is locked out.
    throttle = app.state.login_throttle
    assert all(throttle.retry_after(f"ghost{i}") == 0.0 for i in range(attempts))
    # ...but the unkeyed counter saw every one of them (visibility, not refusal).
    assert app.state.login_failures.count == attempts


def test_register_is_gated_by_the_same_hash_ceiling(monkeypatch):
    """/register hashes too and is auth-exempt: gating only /login would just
    move the flood one route across."""
    app = create_app()
    app.state.hash_limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=0, wait_timeout=0.0
    )
    with TestClient(app) as c:
        with app.state.hash_limiter.reserve():  # occupy the only slot
            r = c.post(
                "/register", data={"username": "newbie", "password": "password123"}
            )
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
        assert db.get_user_by_username("newbie") is None
        # Slot free again: registration works normally.
        assert c.post(
            "/register", data={"username": "newbie", "password": "password123"}
        ).status_code == 200
        assert db.get_user_by_username("newbie") is not None


def test_login_shed_response_is_generic_and_costs_no_hash(monkeypatch):
    app = create_app()
    app.state.hash_limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=0, wait_timeout=0.0
    )
    with TestClient(app) as c:
        c.post("/register", data={"username": "owner", "password": "password123"})
        c.post("/logout")

        def boom(*a, **kw):
            raise AssertionError("no password hash may run once shedding")

        monkeypatch.setattr(auth, "verify_password", boom)
        monkeypatch.setattr(auth, "dummy_verify", boom)
        with app.state.hash_limiter.reserve():
            known = c.post(
                "/login", data={"username": "owner", "password": "password123"}
            )
            unknown = c.post(
                "/login", data={"username": "ghost", "password": "password123"}
            )
    assert known.status_code == unknown.status_code == 503
    # No username-enumeration oracle in the shed path.
    assert known.text == unknown.text


def test_login_and_register_refuse_cross_origin_browser_posts(monkeypatch):
    """The drive-by: a page on evil.example.com POSTs a form at
    http://localhost:8000/login. It can't read the reply, but pre-fix the
    server had already paid ~128 MiB for it."""
    app = create_app()
    with TestClient(app) as c:
        def boom(*a, **kw):
            raise AssertionError("cross-origin post must not reach the hasher")

        monkeypatch.setattr(auth, "verify_password", boom)
        monkeypatch.setattr(auth, "dummy_verify", boom)
        monkeypatch.setattr(auth, "hash_password", boom)
        for path in ("/login", "/register"):
            r = c.post(
                path,
                data={"username": "victim", "password": "password123"},
                headers={"origin": "https://evil.example.com"},
            )
            assert r.status_code == 403, path


def test_login_accepts_same_host_origin_and_the_tailnet_origin(monkeypatch):
    """The Origin check compares HOSTS only, on purpose: `tailscale serve`
    terminates TLS and forwards plain http to this loopback socket, so the
    browser's https Origin legitimately differs in scheme and port from the
    request the app sees. A strict comparison would lock the owner out."""
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", "box.example.ts.net")
    app = create_app()
    with TestClient(app) as c:
        c.post("/register", data={"username": "owner", "password": "password123"})
        c.post("/logout")
        same_host = c.post(
            "/login",
            data={"username": "owner", "password": "password123"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert same_host.status_code == 303  # logged in
        c.post("/logout")
        tailnet = c.post(
            "/login",
            data={"username": "owner", "password": "password123"},
            headers={"origin": "https://box.example.ts.net"},
            follow_redirects=False,
        )
        assert tailnet.status_code == 303


def test_normal_login_still_works_without_an_origin_header(client):
    _register(client)
    client.post("/logout")
    r = client.post(
        "/login", data={"username": "tester", "password": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


# ------------------------- M1 (review) zwift_id path-traversal confinement
#
# The zwift id is joined onto the Zwift Workouts root as a folder name and the
# exporters makedirs() the result, so an id like "../../.." was an
# arbitrary-directory-create plus arbitrary .zwo write outside the root.

@pytest.mark.parametrize("bad", [
    "../../etc", "..", ".", "/etc", "a/b", "a\\b", "C:\\Windows",
    "with:colon", "nul\0byte", "x" * 65,
])
def test_safe_zwift_id_rejects_anything_that_is_not_a_bare_folder_name(bad):
    assert paths.safe_zwift_id(bad) is None


@pytest.mark.parametrize("good", ["1234567", "me", "not a number", "rider-1"])
def test_safe_zwift_id_accepts_plain_folder_names(good):
    assert paths.safe_zwift_id(good) == good


def test_settings_rejects_traversing_zwift_id(client):
    _register(client)
    r = client.post("/settings", data={"zwift_id": "../../../../tmp/pwned"})
    assert r.status_code == 200
    assert "plain folder name" in r.text
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["zwift_id"] is None


def test_settings_still_saves_a_normal_zwift_id(client):
    _register(client)
    r = client.post("/settings", data={"zwift_id": "1234567"})
    assert r.status_code == 200
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["zwift_id"] == "1234567"


def test_save_user_settings_refuses_a_traversing_zwift_id(user_id):
    db.save_user_settings(user_id, {"zwift_id": "1234567"})
    db.save_user_settings(user_id, {"zwift_id": "../../escape"})
    # The bad write is dropped; the previous good value survives.
    assert db.get_user_settings(user_id)["zwift_id"] == "1234567"


def test_stored_traversing_zwift_id_cannot_escape_the_workouts_root(user_id):
    """The stored-before-the-fix case: validating on write is not enough.

    The id is never joined onto a root, so with a real player folder present
    the export lands in that folder - inside the root, exactly where an unset
    id would have put it - and the traversal component appears nowhere.
    """
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])
    player = os.path.join(root, "1234567")
    os.mkdir(player)
    escape_rel = "../../pwned-by-zwift-id"
    _poison_setting(user_id, "zwift_id", escape_rel)
    stored = db.get_user_settings(user_id)["zwift_id"]

    target = os.path.realpath(paths.workouts_dir(stored))
    assert target == os.path.realpath(player)
    assert os.path.commonpath([target, root]) == root

    # End to end: the exporter writes, and makedirs(), inside the root only.
    result = zwo.write_plan_to_zwift(
        [{"date": "2026-07-07", "name": "Test", "zwo": "<workout_file/>"}], stored,
    )
    written = os.path.realpath(result["paths"][0])
    assert os.path.commonpath([written, root]) == root
    assert not os.path.exists(os.path.join(root, escape_rel))


def test_stored_traversing_zwift_id_with_no_player_folder_is_refused(user_id):
    """And with nothing to detect there is no fallback folder to write into.

    paths.workouts_dir() used to answer <root>/me for exactly this input, so
    the export "succeeded" into a folder Zwift never reads.
    """
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])
    _poison_setting(user_id, "zwift_id", "../../pwned-by-zwift-id")
    stored = db.get_user_settings(user_id)["zwift_id"]

    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        zwo.write_plan_to_zwift(
            [{"date": "2026-07-07", "name": "Test", "zwo": "<workout_file/>"}], stored,
        )
    assert excinfo.value.reason == "missing"
    assert os.listdir(root) == []
    assert not os.path.exists(os.path.join(os.path.dirname(root), "pwned-by-zwift-id"))


def test_stored_traversing_zwift_id_is_not_resolved_as_an_export_dir(user_id, tmp_path):
    """resolve_export_dir() only accepted an id whose folder EXISTS - so an
    attacker pointed it at any directory on the machine that does."""
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])
    outside = tmp_path / "outside-target"
    outside.mkdir()
    _poison_setting(user_id, "zwift_id", f"../{outside.name}")
    stored = db.get_user_settings(user_id)["zwift_id"]
    directory, reason = paths.resolve_export_dir(stored, None)
    assert reason != "zwift_id"
    assert directory is None or os.path.commonpath(
        [os.path.realpath(directory), root]
    ) == root


# ---------------- stored workouts_dir confinement (read side)
#
# The stronger of the two stored-path primitives: zwift_id is ONE component
# joined onto a trusted root, workouts_dir is the whole path, and it reaches
# os.makedirs() + open(..., "w"). paths.workouts_dir() began "if override:
# return override" - no confinement at all - so a poisoned row was an arbitrary
# directory create plus an arbitrary .zwo write. Same argument as activities_dir
# and zwift_id: a row is not trustworthy for being in the DB.

# Per-run and inside tmp_path (a SIBLING of the sandboxed HOME, so it is still
# outside every trusted root - see conftest.isolated_env). Never a fixed machine
# path: an absolute constant makes the assertion below depend on whatever else
# has ever run on this box, and a leftover from a previous run fails the suite
# while the fix is working perfectly.
@pytest.fixture()
def escape_dir(tmp_path):
    return str(tmp_path / "escape" / "deep")


@pytest.mark.skipif(os.name == "nt", reason="POSIX path semantics")
def test_stored_workouts_dir_cannot_escape_the_trusted_roots(user_id, escape_dir):
    """The PoC: poison the row, then do exactly what the export routes do.

    The escaping folder is refused outright rather than swapped for a default:
    a user who configured a folder is told it was rejected instead of having
    their workouts quietly written somewhere else. Nothing is created anywhere.
    """
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])
    os.mkdir(os.path.join(root, "123"))  # a perfectly good default exists...
    _poison_setting(user_id, "workouts_dir", escape_dir)
    _poison_setting(user_id, "zwift_id", "123")
    settings = db.get_user_settings(user_id)
    assert settings["workouts_dir"] == escape_dir  # the row really is poisoned

    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        zwo.write_plan_to_zwift(
            [{"date": "2026-08-05", "name": "pwn", "zwo": "<workout_file/>"}],
            settings.get("zwift_id"),
            workouts_override=settings.get("workouts_dir"),
        )
    assert excinfo.value.reason == "blocked"
    assert excinfo.value.refused
    # Nothing was created outside - and nothing was written inside either.
    assert not os.path.exists(os.path.dirname(escape_dir))
    assert os.listdir(os.path.join(root, "123")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX path semantics")
def test_stored_workouts_dir_outside_roots_is_reported_not_silently_redirected(user_id, escape_dir):
    """resolve_export_dir() says 'blocked' so the UI can tell the user.

    Silently exporting somewhere else would leave a user staring at a Zwift
    folder that never fills up, with only a log line to explain it.
    """
    _poison_setting(user_id, "workouts_dir", escape_dir)
    settings = db.get_user_settings(user_id)
    assert paths.resolve_export_dir(
        settings.get("zwift_id"), settings.get("workouts_dir")
    ) == (None, "blocked")


@pytest.mark.skipif(os.name == "nt", reason="POSIX path semantics")
def test_poisoned_workouts_dir_blocks_export_through_the_exporter(user_id, escape_dir):
    """exporter.sync_plan_exports() is one of the 7 read sites; it must not
    write - or unlink - anything outside the trusted roots."""
    from wattracker import exporter

    _poison_setting(user_id, "workouts_dir", escape_dir)
    result = exporter.sync_plan_exports(user_id)
    assert result["status"] == "blocked"
    assert result["directory"] is None
    assert result["exported"] == 0
    assert not os.path.exists(os.path.dirname(escape_dir))


@pytest.mark.skipif(os.name == "nt", reason="POSIX path semantics")
def test_poisoned_workouts_dir_blocks_the_plan_export_route(client, escape_dir):
    """End to end through HTTP: the route must not create the directory.

    The plan and its workout are seeded deliberately: /plan/{id}/export only
    writes `if workouts`, so against an empty plan the route 404s without ever
    reaching zwo.write_plan_to_zwift and the assertions below hold whether or
    not the fix is present. Seeding is what makes this a real guard.

    The refusal reaches the route as paths.ExportTargetUnavailable and the
    route now renders it: a 200 carrying the 'blocked' message and a link to
    Settings, NOT a 200 reporting a directory the user never configured (the
    old behaviour) and not a 500. The two filesystem assertions below are the
    security invariant and are unchanged from when this test was written.
    """
    _register(client)
    uid = db.get_user_by_username("tester")["id"]
    plan_id = db.create_plan(uid, "P", "2026-08-03", 1)
    db.add_plan_workout(plan_id, uid, "2026-08-05", "pwn", "threshold",
                        3600, 60.0, "<workout_file/>")
    _poison_setting(uid, "workouts_dir", escape_dir)
    _poison_setting(uid, "zwift_id", "123")
    assert db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)  # really exports

    r = client.post(f"/plan/{plan_id}/export")

    assert r.status_code == 200
    assert "will not write to that folder" in r.text
    assert '<a href="/settings">' in r.text
    assert "Exported 1 .zwo" not in r.text  # never claims a bogus success
    assert not os.path.exists(os.path.dirname(escape_dir))
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])
    assert os.listdir(root) == []  # no substitute folder, no stray .zwo


# ---------------- issue #44: the "me" fallback folder
#
# paths.workouts_dir() ended with join(root, safe_zwift_id(id) or "me"), and the
# explicit export routes still pass `settings.get("zwift_id") or "me"`. On a
# real install with no zwift_id that wrote every .zwo into <Documents>\Zwift\
# Workouts\me\ - which Zwift never reads - and reported the path as a success.
# The resolver has no "me" branch left, so even the literal id "me" cannot
# produce that folder; it is treated like any other id whose folder is absent.

def test_export_with_no_zwift_id_refuses_instead_of_writing_into_me(user_id):
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])
    settings = db.get_user_settings(user_id)  # nothing configured at all

    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        zwo.write_plan_to_zwift(
            [{"date": "2026-08-05", "name": "Test", "zwo": "<workout_file/>"}],
            settings.get("zwift_id") or "me",  # what the routes still pass
            workouts_override=settings.get("workouts_dir"),
        )
    assert excinfo.value.reason == "missing"
    assert not excinfo.value.refused
    assert not os.path.exists(os.path.join(root, "me"))
    assert os.listdir(root) == []


def test_a_literal_me_id_lands_in_the_real_player_folder(user_id):
    """And where a player folder does exist, that is where the export goes."""
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])
    player = os.path.join(root, "1234567")
    os.mkdir(player)

    result = zwo.write_plan_to_zwift(
        [{"date": "2026-08-05", "name": "Test", "zwo": "<workout_file/>"}], "me",
    )
    assert result["directory"] == player
    assert not os.path.exists(os.path.join(root, "me"))
    assert len(os.listdir(player)) == 1


def test_write_to_zwift_refuses_the_same_way(user_id):
    """The single-workout writer shares the contract, not just the plan one."""
    root = os.path.realpath(os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"])

    with pytest.raises(paths.ExportTargetUnavailable):
        zwo.write_to_zwift("<workout_file/>", "me", name="Test")
    assert os.listdir(root) == []


def test_stored_workouts_dir_inside_the_trusted_roots_still_works(user_id, home_dir):
    """The confinement must not break the legitimate configured-folder case."""
    good = home_dir / "Documents" / "Zwift" / "Workouts" / "123"
    good.mkdir(parents=True)
    db.save_user_settings(user_id, {"workouts_dir": str(good), "zwift_id": "123"})
    settings = db.get_user_settings(user_id)
    directory, reason = paths.resolve_export_dir(
        settings.get("zwift_id"), settings.get("workouts_dir")
    )
    assert reason == "override"
    assert os.path.realpath(directory) == os.path.realpath(str(good))


# ------------------- M2 (review) POST /activities/rescan confinement
#
# The endpoint scanned AND persisted whatever directory was posted, straight
# past the /settings check.

@pytest.mark.skipif(os.name == "nt", reason="/etc is POSIX-specific")
def test_rescan_rejects_dir_outside_trusted_roots(client):
    _register(client)
    r = client.post("/activities/rescan", data={"activities_dir": "/etc"})
    assert r.status_code == 400
    assert "inside your home directory" in r.json()["error"]
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None
    # No scan was started either. (The status dict is process-global and may
    # still hold an earlier test's entry for this user id - what matters is
    # that no scan of the rejected folder was ever started.)
    status = client.get("/api/scan/status").json()
    assert status.get("directory") != "/etc"


def test_rescan_rejects_traversal_out_of_a_trusted_root(client, tmp_path):
    _register(client)
    outside = tmp_path / "outside-activities"
    outside.mkdir()
    root = os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"]  # a trusted root
    r = client.post(
        "/activities/rescan",
        data={"activities_dir": os.path.join(root, "..", outside.name)},
    )
    assert r.status_code == 400
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None


def test_rescan_still_scans_a_folder_inside_the_trusted_roots(client, tmp_path,
                                                              monkeypatch):
    from wattracker.ingest import importer as importer_mod

    home = os.path.realpath(tmp_path)
    redirect_home(monkeypatch, home)  # setenv("HOME") alone is inert on Windows
    _register(client)
    act_dir = os.path.join(home, "Rides")
    os.mkdir(act_dir)
    open(os.path.join(act_dir, "ride.fit"), "wb").write(b"dummy")
    monkeypatch.setattr(importer_mod, "parse_fit", lambda path: {
        "start_time": "2026-06-01T10:00:00",
        "duration_s": 60,
        "streams": {"time": [None] * 60, "power": [200.0] * 60,
                    "heartrate": [140.0] * 60, "cadence": [90.0] * 60,
                    "distance": list(range(60)), "altitude": [0.0] * 60},
    })
    r = client.post("/activities/rescan", data={"activities_dir": act_dir})
    assert r.status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        status = client.get("/api/scan/status").json()
        if not status.get("running"):
            break
        time.sleep(0.02)
    assert status["directory"] == act_dir
    assert status["imported"] == 1
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] == act_dir


def test_stored_activities_dir_outside_the_roots_is_never_scanned(user_id, tmp_path):
    """The stored-before-the-fix case for the scanner: a row planted through
    the old rescan hole must stop being obeyed, including by the daily sweep."""
    from wattracker.ingest import importer as importer_mod

    outside = tmp_path / "outside-activities"
    outside.mkdir()
    (outside / "ride.fit").write_bytes(b"dummy")
    _poison_setting(user_id, "activities_dir", str(outside))

    result = importer_mod.scan_activities(user_id)
    assert result["found"] == 0
    assert result["directory"] is None
    assert db.list_activities(user_id) == []


# ------------------- LoginAttemptCounter (unkeyed login-failure visibility)
#
# The counter exists because auth.LoginThrottle is keyed by username: an
# attacker who never reuses a username leaves every key at one failure and the
# throttle stays silent. A single unkeyed count is the only thing that sees
# that shape. It is visibility ONLY - it must never refuse anything, because a
# global refusal would let one bad client lock the legitimate owner out.

def test_login_attempt_counter_counts_and_returns_the_running_total():
    counter = server_mod.LoginAttemptCounter()
    assert counter.count == 0
    assert counter.record_failure() == 1
    assert counter.record_failure() == 2
    assert counter.count == 2


def test_login_attempt_counter_is_unkeyed():
    """No username/address argument exists: rotating either cannot split the
    tally into buckets that each stay under the threshold."""
    import inspect

    sig = inspect.signature(server_mod.LoginAttemptCounter.record_failure)
    assert list(sig.parameters) == ["self"]


def test_login_attempt_counter_refuses_nothing():
    """record_failure() never raises and never signals 'deny' - the return is a
    monotonically rising count, not a verdict."""
    counter = server_mod.LoginAttemptCounter()
    totals = [counter.record_failure() for _ in range(200)]
    assert totals == list(range(1, 201))
    assert all(isinstance(t, int) for t in totals)
    assert counter.count == 200


def test_login_attempt_counter_is_threadsafe():
    """Concurrent failures must not lose counts to a read-modify-write race."""
    counter = server_mod.LoginAttemptCounter()
    threads = [
        threading.Thread(target=lambda: [counter.record_failure() for _ in range(50)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert counter.count == 8 * 50


def test_record_login_failure_logs_every_threshold_th_attempt(caplog):
    """The log line is the whole point of the counter: it must fire on each
    multiple of the threshold and stay quiet in between."""
    counter = server_mod.LoginAttemptCounter()
    threshold = server_mod.LOGIN_FAILURE_LOG_THRESHOLD
    assert threshold > 1  # otherwise 'in between' is not a state

    logged_at = []
    with caplog.at_level("WARNING", logger="wattracker.server"):
        for _ in range(threshold * 3):
            caplog.clear()
            total = server_mod._record_login_failure(counter)
            if any("refused login attempts since start" in r.message
                   for r in caplog.records):
                logged_at.append(total)

    assert logged_at == [threshold, threshold * 2, threshold * 3]


def test_record_login_failure_rollover_keeps_counting_past_the_threshold(caplog):
    """Rollover is a modulo, not a reset: the total keeps rising and the
    message reports the true running total, not the position within a window."""
    counter = server_mod.LoginAttemptCounter()
    threshold = server_mod.LOGIN_FAILURE_LOG_THRESHOLD

    with caplog.at_level("WARNING", logger="wattracker.server"):
        for _ in range(threshold * 2):
            server_mod._record_login_failure(counter)

    assert counter.count == threshold * 2
    messages = [r.getMessage() for r in caplog.records
                if "refused login attempts since start" in r.getMessage()]
    assert messages == [
        f"{threshold} refused login attempts since start (all usernames)",
        f"{threshold * 2} refused login attempts since start (all usernames)",
    ]


def test_record_login_failure_returns_the_total_and_never_raises():
    counter = server_mod.LoginAttemptCounter()
    threshold = server_mod.LOGIN_FAILURE_LOG_THRESHOLD
    # Crossing the threshold must not change the return contract.
    totals = [server_mod._record_login_failure(counter)
              for _ in range(threshold + 3)]
    assert totals == list(range(1, threshold + 4))


def test_rotating_usernames_trip_the_counter_but_never_the_throttle(client):
    """The exact shape the counter exists for, end to end through /login.

    Every attempt uses a fresh username, so the per-username throttle stays at
    zero for all of them - and every one of those users can still log in. The
    unkeyed counter is what noticed.
    """
    _register(client, "victim", "password123")
    app = client.app
    attempts = server_mod.LOGIN_FAILURE_LOG_THRESHOLD + 2
    before = app.state.login_failures.count

    for i in range(attempts):
        r = client.post(
            "/login", data={"username": f"ghost{i}", "password": "wrong-password"}
        )
        assert r.status_code == 200  # refused the login, but never rate-limited

    assert app.state.login_failures.count == before + attempts
    throttle = app.state.login_throttle
    assert all(throttle.retry_after(f"ghost{i}") == 0.0 for i in range(attempts))
    # The counter refused nothing: the real owner is not locked out.
    assert throttle.retry_after("victim") == 0.0
    r = client.post(
        "/login", data={"username": "victim", "password": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_the_counter_also_sees_shed_attempts(client):
    """A flood answered with 503 by the hash limiter is still a refused login
    attempt and must still be counted - otherwise the attack that motivated the
    limiter is exactly the one the counter goes blind to."""
    app = client.app
    app.state.hash_limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=0, wait_timeout=0.0
    )
    before = app.state.login_failures.count
    with app.state.hash_limiter.reserve():  # occupy the only slot
        r = client.post(
            "/login", data={"username": "ghost", "password": "password123"}
        )
    assert r.status_code == 503
    assert app.state.login_failures.count == before + 1


def test_shedding_does_not_lock_the_owner_out_through_the_throttle(client):
    """Shedding must NOT call throttle.record_failure.

    Otherwise the hash limiter becomes a lockout weapon: an attacker floods
    until every request sheds, and each shed 503 counts as a failure against
    whatever username was posted - so naming the real owner locks them out of
    their own machine without ever guessing a password.
    """
    _register(client, "owner", "password123")
    app = client.app
    app.state.hash_limiter = auth.PasswordHashLimiter(
        max_concurrent=1, max_waiting=0, wait_timeout=0.0
    )
    # Comfortably past LoginThrottle's default threshold of 5 consecutive
    # failures, all naming the SAME username - the shape that would lock out.
    shed = 12
    with app.state.hash_limiter.reserve():  # occupy the only slot
        for _ in range(shed):
            r = client.post(
                "/login", data={"username": "owner", "password": "wrong-password"}
            )
            assert r.status_code == 503

    # Control: that many genuine failures DOES lock a username out, so the
    # count above is unambiguously enough to trip the throttle.
    for _ in range(shed):
        app.state.login_throttle.record_failure("control-user")
    assert app.state.login_throttle.retry_after("control-user") > 0

    # The owner, however, is untouched and can still log in.
    assert app.state.login_throttle.retry_after("owner") == 0.0
    r = client.post(
        "/login", data={"username": "owner", "password": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# ------------------------- .zwo filenames cannot leave the export folder
#
# zwo.plan_filename is byte-identical on main, where it only ever sees dates
# from _dt.date.fromisoformat. The server/client split is what makes it
# reachable from a manifest that crossed a network, so the guard lands here -
# but it hardens the single-machine app in the same move.


def test_a_plan_filename_is_always_one_path_component():
    """The date leads the filename and used to be interpolated raw.

    An absolute date is enough on its own; no ``..`` is required.
    """
    import ntpath

    for hostile in (
        "../../../../.config/autostart/pwn",
        "/etc/cron.d/x",
        "..\\..\\evil",
        "2026-07-07/../../escape",
        "C:\\Windows\\System32\\x",
        "",
        None,
    ):
        fname = zwo.plan_filename(hostile, "VO2 5x4")
        assert os.path.basename(fname) == fname, hostile
        assert ntpath.basename(fname) == fname, hostile
        assert not os.path.isabs(fname), hostile
        assert not ntpath.isabs(fname), hostile

    # A real date is untouched, so the sanitiser is not paying for itself with
    # mangled names on the path everybody actually uses.
    assert zwo.plan_filename("2026-07-07", "VO2 5x4") == "2026-07-07 VO2 5x4.zwo"


def test_the_writer_refuses_a_filename_that_would_escape(tmp_path, monkeypatch):
    """The last check before open(..., "w"), asserted with the sanitiser bypassed.

    Confining the DIRECTORY is worth nothing if the name joined onto it can
    walk back out, so this guard has to hold independently of whatever
    produced the name.
    """
    target = tmp_path / "Workouts"
    target.mkdir()
    monkeypatch.setattr(zwo, "plan_filename", lambda d, n: "../escaped.zwo")
    monkeypatch.setattr(
        "wattracker.paths.workouts_dir", lambda *a, **k: str(target)
    )

    with pytest.raises(ValueError):
        zwo.write_plan_to_zwift(
            [{"date": "2026-07-07", "name": "x", "zwo": "<workout_file/>"}], None
        )
    assert not (tmp_path / "escaped.zwo").exists()
    assert list(target.iterdir()) == []


def test_an_upload_cannot_claim_to_be_an_in_app_ride(client, monkeypatch):
    """The recorded filename is the rider's string now, and it classifies rides.

    ``ingest_file`` records the uploaded name rather than the temp file's, and
    ``db.IN_APP_FILENAME_SQL`` reads that column: 'Ride <date> ...' WITHOUT a
    .fit extension means the ride was recorded in-app. An upload is not, so it
    must not be able to take that shape.

    The invariant is "ends in .fit", and it has to be, because the first fix
    here required only SOME extension: 'Ride <date> x.gpx' and 'Ride <date> x.'
    both sailed through it and both classify as in-app, in the SQL and in
    is_in_app_activity alike. The two classifiers agreed with each other and
    were both wrong, which is why this asserts on the stored name as well as on
    what the classifiers make of it.
    """
    from wattracker.ingest import importer

    minute = [0]

    def _parsed(path):
        minute[0] += 1
        return {
            "start_time": f"2026-06-01T10:{minute[0]:02d}:00", "duration_s": 1800,
            "streams": {"time": [None] * 1800, "power": [200.0] * 1800},
        }

    monkeypatch.setattr(importer, "parse_fit", _parsed)
    _register(client)
    uid = db.get_user_by_username("tester")["id"]

    for uploaded in (
        "Ride 2026-06-01 09-00-00",    # no extension at all
        "Ride 2026-06-01 09-00-00.gpx",  # an extension, but not .fit
        "Ride 2026-06-01 09-00-00.",   # an extension splitext does not see
    ):
        activity_id = importer.ingest_upload(uid, uploaded, b"x")
        assert activity_id is not None, uploaded
        stored = db.get_activity(uid, activity_id)
        assert stored["filename"].endswith(".fit"), uploaded
        # Both classifiers - the Python one and the SQL the migrations run.
        assert importer.is_in_app_activity(stored["filename"]) is False, uploaded
    conn = db.connect(None)
    try:
        matched = conn.execute(
            f"SELECT COUNT(*) AS n FROM activities WHERE {db.IN_APP_FILENAME_SQL}"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert matched == 0
