"""The connector transport: auth at the handshake, RPC framing, registry.

Everything here goes through the real /connector/ws route and the real RpcPeer
framing - the only thing standing in for production is that the connector runs
in a thread rather than a separate process.
"""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from wattracker import connectorauth, connectorhub, db, rpc  # noqa: E402
from wattracker.backend.remote import RemoteBackend  # noqa: E402
from wattracker.server import create_app  # noqa: E402

from conftest_connector import attach_connector  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_hub():
    connectorhub.reset()
    yield
    connectorhub.reset()


@pytest.fixture()
def zwift_home(tmp_path):
    (tmp_path / "Activities").mkdir()
    (tmp_path / "Workouts").mkdir()
    return tmp_path


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


# ------------------------------------------------------------------- auth
def test_connector_without_a_token_is_refused(client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/connector/ws") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_connector_with_a_bad_token_is_refused(client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/connector/ws", headers={"Authorization": "Bearer " + "A" * 43}
        ) as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_a_revoked_token_stops_working(client):
    uid = _register(client)
    device_id, token = connectorauth.generate_token(uid, "Zwift PC")
    connectorauth.revoke(uid, device_id)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/connector/ws", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.receive_json()


def test_rejections_are_counted_but_never_refuse_a_valid_token(client):
    uid = _register(client)
    for _ in range(5):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/connector/ws", headers={"Authorization": "Bearer " + "B" * 43}
            ) as ws:
                ws.receive_json()
    assert client.app.state.connector_failures.count == 5

    # The whole point of counting rather than throttling: a real connector
    # still attaches immediately afterwards.
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")
    with client.websocket_connect(
        "/connector/ws", headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        assert ws.receive_json()["event"] == "hello"


def test_attaching_registers_the_session(client, zwift_home):
    uid = _register(client)
    assert connectorhub.is_attached(uid) is False
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        assert connectorhub.is_attached(uid) is True
    assert connectorhub.is_attached(uid) is False


def test_a_second_connector_displaces_the_first(client, zwift_home):
    uid = _register(client)
    first, _c1 = attach_connector(client, uid, zwift_home, label="Desktop")
    with first:
        original = connectorhub.require(uid)
        assert original.label == "Desktop"
        second, _c2 = attach_connector(client, uid, zwift_home, label="Laptop")
        with second:
            current = connectorhub.require(uid)
            assert current.label == "Laptop"
            assert current is not original
            # The displaced session is dead, and calls against it say so
            # rather than hanging until a timeout.
            assert original.closed is True
            with pytest.raises(rpc.ConnectorUnavailable):
                original.call_sync("paths.workouts_root")
            # The survivor still works.
            assert RemoteBackend(uid).list_activities(
                str(zwift_home / "Activities")
            ).exists is True


def test_displacement_uses_a_close_code_the_client_can_act_on(client, zwift_home):
    """Two connectors on one account must not fight forever.

    Displacement closes with WS_REPLACED rather than a normal 1000, because
    reconnecting is the wrong response to it: each connector would evict the
    other and be evicted straight back. The client stops on this code and says
    why - it is a configuration mistake, not a network problem.
    """
    uid = _register(client)
    closed_with = []

    class _Recorder:
        def __init__(self):
            self.user_id = uid
            self.label = "old"
            self.closed = False

        def close(self, reason="", code=1000):
            self.closed = True
            closed_with.append(code)

    old = _Recorder()
    connectorhub._sessions[uid] = old
    try:
        attached, _config = attach_connector(client, uid, zwift_home, label="new")
        with attached:
            assert connectorhub.require(uid).label == "new"
    finally:
        connectorhub.reset()

    assert closed_with == [rpc.WS_REPLACED]
    assert old.closed is True


def test_closing_a_session_hangs_up_the_socket(client, zwift_home):
    """A closed session must actually close the websocket.

    Without this the connector never learns it was displaced - from its side
    the connection still looks healthy - so it neither reconnects nor stops,
    and the socket leaks for the life of the process.
    """
    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        session = connectorhub.require(uid)
        assert session._closer is not None
        session.close("test")
        assert session.closed is True
        assert connectorhub.is_attached(uid) is False


# -------------------------------------------------------------------- rpc
def test_round_trip_reaches_the_real_handlers(client, zwift_home):
    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        backend = RemoteBackend(uid)
        listing = backend.list_activities(str(zwift_home / "Activities"))
        assert listing.exists is True
        assert listing.files == []


def test_errors_from_a_handler_come_back_as_rpc_errors(client, zwift_home):
    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        backend = RemoteBackend(uid)
        with pytest.raises(rpc.RpcError):
            # Outside the activities folder: the connector must refuse rather
            # than read an arbitrary local file.
            with backend.readable_activity("/etc/passwd"):
                pass


def test_calls_without_a_connector_are_unavailable_not_a_crash(client):
    uid = _register(client)
    backend = RemoteBackend(uid)
    with pytest.raises(rpc.ConnectorUnavailable):
        backend.list_activities("/anywhere")


def test_unknown_methods_are_reported_not_fatal(client, zwift_home):
    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        session = connectorhub.require(uid)
        with pytest.raises(rpc.RpcError, match="unknown method"):
            session.call_sync("nope.not.a.method")
        # The connection survives it.
        assert RemoteBackend(uid).list_activities(
            str(zwift_home / "Activities")
        ).exists is True


# --------------------------------------------------------------- framing
def test_oversized_frames_are_refused_by_the_codec():
    with pytest.raises(rpc.ProtocolError):
        rpc.decode("x" * (rpc.MAX_FRAME_BYTES + 1))
    with pytest.raises(rpc.ProtocolError):
        rpc.encode({"blob": "x" * (rpc.MAX_FRAME_BYTES + 1)})


def test_non_object_frames_are_refused():
    for bad in ('"a string"', "[1,2,3]", "null", "not json at all", b"\xff\xfe"):
        with pytest.raises(rpc.ProtocolError):
            rpc.decode(bad)
