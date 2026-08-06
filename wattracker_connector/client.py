"""Holds the connection to the server and answers what it asks.

The connector dials out and keeps one WebSocket open. When it drops - server
restart, laptop sleep, flaky wifi - it reconnects with exponential backoff and
carries on. There is nothing to resynchronise on reconnect: the server drives
every exchange, and its own ``scanned_files`` cache means the next scan picks
up exactly where the last one stopped.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from wattracker import rpc

from .ble_handlers import BleState, build_ble_handlers
from .buffer import upload_pending
from .handlers import ConnectorConfig, build_handlers

log = logging.getLogger(__name__)

# Reconnect backoff. Starts quick, because the overwhelmingly common case is a
# server restart that is over in seconds, and tops out low enough that a
# machine left running overnight rejoins promptly once the server is back.
_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 60.0
_BACKOFF_FACTOR = 2.0

# Sent by the server as soon as it accepts. Its absence within this many
# seconds means we are talking to something that is not a wattracker server.
_HELLO_TIMEOUT_S = 20.0


class _Replaced(Exception):
    """The server closed us because another connector took over the account."""


class ConnectorStatus:
    """What the tray icon shows. Plain attributes, read from another thread."""

    def __init__(self) -> None:
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_connected_at: Optional[str] = None
        self.server_url: Optional[str] = None


def websocket_url(server_url: str) -> str:
    """Turn the server's base URL into the connector endpoint's ws:// URL."""
    parts = urlsplit(server_url.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(
            "server URL must be absolute, e.g. http://192.168.1.10:8000"
        )
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(
        parts.scheme.lower()
    )
    if scheme is None:
        raise ValueError(f"unsupported server URL scheme: {parts.scheme}")
    return urlunsplit((scheme, parts.netloc, "/connector/ws", "", ""))


class _ClientSocket:
    """Gives RpcPeer its two methods over a `websockets` connection."""

    def __init__(self, connection) -> None:
        self._connection = connection

    async def send_text(self, text: str) -> None:
        await self._connection.send(text)

    async def receive_text(self) -> str:
        message = await self._connection.recv()
        if isinstance(message, bytes):
            return message.decode("utf-8", errors="replace")
        return message


class Connector:
    """The connector's run loop."""

    def __init__(
        self,
        server_url: str,
        token: str,
        config: ConnectorConfig,
        status: Optional[ConnectorStatus] = None,
        extra_handlers: Optional[Dict[str, Callable]] = None,
    ) -> None:
        self.server_url = server_url
        self.token = token
        self.config = config
        self.status = status or ConnectorStatus()
        self.status.server_url = server_url
        self._handlers = build_handlers(config)
        # The BLE half needs to push events, so it is handed a sender that
        # resolves the live peer at call time - the peer is replaced on every
        # reconnect, and a captured one would go stale.
        self.ble = BleState()
        self._handlers.update(build_ble_handlers(self.ble, self._send_event))
        if extra_handlers:
            self._handlers.update(extra_handlers)
        self._peer: Optional[rpc.RpcPeer] = None
        self._stop = asyncio.Event()

    @property
    def peer(self) -> Optional[rpc.RpcPeer]:
        """The live peer, for pushing events (ride telemetry) at the server."""
        return self._peer

    async def _send_event(self, event: str, **fields) -> None:
        peer = self._peer
        if peer is None:
            raise rpc.ConnectorUnavailable("not connected")
        await peer.send_event(event, **fields)

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        """Connect, serve, and reconnect until stopped."""
        backoff = _BACKOFF_START_S
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = _BACKOFF_START_S  # a clean session resets the clock
            except asyncio.CancelledError:
                raise
            except _Replaced as exc:
                # Another connector took this account over. Reconnecting would
                # evict it and get us evicted right back, forever - so stop,
                # and say why, because this is a configuration mistake and not
                # a network problem.
                self.status.connected = False
                self.status.last_error = str(exc)
                log.error(
                    "another connector has taken over this account - stopping. "
                    "Only one connector may run per account; quit the other "
                    "one, or pair this machine as its own device."
                )
                self._stop.set()
                break
            except Exception as exc:
                self.status.connected = False
                self.status.last_error = str(exc)
                log.warning("connector session ended: %s", exc)
            if self._stop.is_set():
                break
            # Jitter so a fleet of connectors does not stampede a server that
            # just came back up.
            delay = min(backoff, _BACKOFF_MAX_S) * (0.5 + random.random())
            log.info("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_S)

    async def _session(self) -> None:
        # Imported here, not at module scope, so the rest of this package
        # stays importable (and testable) without the dependency present.
        import websockets

        url = websocket_url(self.server_url)
        log.info("connecting to %s", url)
        try:
            await self._connected_session(websockets, url)
        except websockets.exceptions.ConnectionClosed as exc:
            if exc.rcvd is not None and exc.rcvd.code == rpc.WS_REPLACED:
                raise _Replaced(str(exc)) from exc
            raise

    async def _connected_session(self, websockets, url: str) -> None:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=rpc.MAX_FRAME_BYTES,
            open_timeout=_HELLO_TIMEOUT_S,
        ) as connection:
            socket = _ClientSocket(connection)
            peer = rpc.RpcPeer(socket)
            self._peer = peer
            try:
                hello = rpc.decode(
                    await asyncio.wait_for(
                        socket.receive_text(), timeout=_HELLO_TIMEOUT_S
                    )
                )
                if hello.get("event") != "hello":
                    raise rpc.ProtocolError("server did not greet us")
                if hello.get("protocol") != rpc.PROTOCOL_VERSION:
                    raise rpc.ProtocolError(
                        f"server speaks protocol {hello.get('protocol')}, "
                        f"this connector speaks {rpc.PROTOCOL_VERSION} - "
                        "update whichever is older"
                    )
                self.status.connected = True
                self.status.last_error = None
                log.info("connected")
                # First thing after every connect, before serving anything: a
                # ride buffered while we were away is the most perishable
                # thing this process holds, and there is no reason to make it
                # wait behind a scan.
                await self._flush_buffered_ride()
                await self._serve(socket, peer)
            finally:
                self._peer = None
                self.status.connected = False
                peer.abandon("disconnected")
                # Never leave the radio held across a reconnect: the server
                # has no session to resume into, and a half-held adapter is
                # what stops the next scan from finding anything.
                await self.ble.teardown()

    async def _flush_buffered_ride(self) -> None:
        """Upload a ride recorded while the server was unreachable."""
        try:
            await asyncio.to_thread(
                upload_pending, self.server_url, self.token, self.ble.buffer
            )
        except Exception:
            # Never let this stop the session starting - the buffer keeps the
            # ride and the next reconnect tries again.
            log.warning("could not upload the buffered ride", exc_info=True)

    async def _serve(self, socket, peer: rpc.RpcPeer) -> None:
        """Answer requests until the socket closes or we are told to stop."""
        while not self._stop.is_set():
            message = rpc.decode(await socket.receive_text())
            if peer.resolve(message):
                continue
            if "method" in message:
                # Served as a task so a slow call (a big file read, a BLE
                # scan) does not stall the ones behind it.
                asyncio.create_task(peer.serve(message, self._handlers))
