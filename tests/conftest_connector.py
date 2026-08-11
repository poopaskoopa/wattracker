"""An in-process connector, attached over the real /connector/ws socket.

This is what makes the whole server/client split testable without a second
machine: the connector's own handlers run against a temp directory pretending
to be a Zwift install, and they are reached through the genuine WebSocket
route, RPC framing and token auth - not a stub of any of it.

Starlette's WebSocketTestSession is synchronous and lives in the calling
thread, so the connector loop runs in a background thread while the test body
carries on. That mirrors production, where the connector is a separate process
entirely.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Optional

from wattracker import connectorauth, rpc
from wattracker_connector.handlers import ConnectorConfig, build_handlers


class FakeConnector:
    """Runs the real connector handlers against a test websocket session."""

    def __init__(self, session, handlers, on_error=None):
        self._session = session
        self._handlers = handlers
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.errors = []
        self._on_error = on_error

    def start(self) -> "FakeConnector":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._serve())
        except Exception as exc:  # the socket closing ends the loop
            self.errors.append(exc)
        finally:
            loop.close()

    async def _serve(self) -> None:
        peer = rpc.RpcPeer(_TestSocket(self._session))
        while not self._stop.is_set():
            message = rpc.decode(self._session.receive_text())
            if "method" in message:
                await peer.serve(message, self._handlers)

    def stop(self) -> None:
        self._stop.set()


class _TestSocket:
    """Gives RpcPeer its two methods over a synchronous test session.

    The sends are blocking calls made from the connector thread, which is
    exactly how the real client behaves from its own process.
    """

    def __init__(self, session) -> None:
        self._session = session

    async def send_text(self, text: str) -> None:
        self._session.send_text(text)

    async def receive_text(self) -> str:
        return self._session.receive_text()


def attach_connector(client, user_id, zwift_home, label="Test PC"):
    """Pair a device, open /connector/ws, and run a connector on it.

    Returns ``(context_manager, config)``. Use as::

        with attach_connector(client, uid, tmp_path) as conn:
            ...
    """
    _device_id, token = connectorauth.generate_token(user_id, label)
    config = ConnectorConfig(
        activities_dir=str(zwift_home / "Activities"),
        workouts_dir=str(zwift_home / "Workouts"),
    )
    return _Attached(client, token, build_handlers(config)), config


class _Attached:
    def __init__(self, client, token, handlers):
        self._client = client
        self._token = token
        self._handlers = handlers
        self._ws = None
        self._connector = None

    def __enter__(self):
        self._ws = self._client.websocket_connect(
            "/connector/ws", headers={"Authorization": f"Bearer {self._token}"}
        )
        session = self._ws.__enter__()
        # The server greets first; drain it so the connector loop starts clean.
        session.receive_json()
        self._connector = FakeConnector(session, self._handlers).start()
        return self._connector

    def __exit__(self, *exc):
        if self._connector is not None:
            self._connector.stop()
        return self._ws.__exit__(*exc)
