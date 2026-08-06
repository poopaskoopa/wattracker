"""Server-side registry of the connectors currently attached.

One process, one dict - the same assumption ``server._scan_status`` already
makes, and for the same reason: this app is a single uvicorn process, and a
connector's identity is a live socket, which cannot be shared across processes
anyway.

The awkward part this module exists to solve is threading. A connector's
socket lives on the event loop, but almost everything that wants to reach it
does not: ``scan_activities`` runs in a plain ``threading.Thread`` started by
``_start_user_scan``, the daily sweep runs under ``asyncio.to_thread``, and
FastAPI runs ``def`` routes in its threadpool. So the ``Backend`` interface is
synchronous, and this module provides the bridge - ``call_sync`` hands the
coroutine to the loop and blocks the calling worker thread until it answers.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from .rpc import WS_REPLACED, ConnectorUnavailable, RpcPeer

_log = logging.getLogger(__name__)

_lock = threading.Lock()
_sessions: "Dict[int, ConnectorSession]" = {}


class ConnectorSession:
    """One attached connector, and the means to call it from any thread."""

    def __init__(
        self,
        user_id: int,
        device_id: int,
        label: str,
        peer: RpcPeer,
        loop: asyncio.AbstractEventLoop,
        closer=None,
    ) -> None:
        self.user_id = user_id
        self.device_id = device_id
        self.label = label
        self.peer = peer
        self._loop = loop
        self._loop_thread_id = threading.get_ident()
        # Awaitable that shuts the underlying websocket. Without it a displaced
        # connector keeps a dead socket open and never reconnects: from its end
        # the connection still looks healthy.
        self._closer = closer
        self.closed = False
        # Where inbound ble.sample events land while a ride is connected. Set
        # by remote_ble.connect_sensors, cleared when the ride ends. None means
        # "no ride in progress", and a stray sample is simply dropped.
        self.ble_sink = None

    # ------------------------------------------------------------- calling
    async def call(
        self, method: str, params: Optional[dict] = None,
        *, timeout: Optional[float] = None,
    ) -> Any:
        """Call the connector from the event loop."""
        if self.closed:
            raise ConnectorUnavailable("connector disconnected")
        return await self.peer.call(method, params, timeout=timeout)

    def call_sync(
        self, method: str, params: Optional[dict] = None,
        *, timeout: Optional[float] = None,
    ) -> Any:
        """Call the connector from a worker thread, blocking until it answers.

        Refuses to run on the event loop thread rather than deadlocking there:
        scheduling onto the loop we are currently *blocking* would wait
        forever, and a hang is far harder to diagnose than an exception.
        """
        if threading.get_ident() == self._loop_thread_id:
            raise RuntimeError(
                "call_sync would deadlock on the event loop thread - await "
                "ConnectorSession.call instead"
            )
        if self.closed:
            raise ConnectorUnavailable("connector disconnected")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.peer.call(method, params, timeout=timeout), self._loop
            )
        except RuntimeError as exc:  # loop already shut down
            raise ConnectorUnavailable("server is shutting down") from exc
        # No timeout here on purpose: RpcPeer.call already bounds the wait and
        # raises ConnectorUnavailable, and abandon() fails every in-flight call
        # the moment the socket drops. A second, shorter deadline here would
        # only orphan requests the far end is still working on.
        return future.result()

    def close(self, reason: str = "connector disconnected", code: int = 1000) -> None:
        """Mark dead, fail in-flight calls, and hang up with ``code``.

        Safe from any thread and idempotent: closing a session twice (the
        displacement path and then the socket handler's own finally block)
        must not raise.
        """
        already = self.closed
        self.closed = True
        self.peer.abandon(reason)
        if already or self._closer is None:
            return
        closer, self._closer = self._closer, None
        try:
            if threading.get_ident() == self._loop_thread_id:
                self._loop.create_task(closer(code))
            else:
                asyncio.run_coroutine_threadsafe(closer(code), self._loop)
        except RuntimeError:
            # Loop already stopped - the socket is going away regardless.
            _log.debug("could not close connector socket", exc_info=True)


# ---------------------------------------------------------------- registry
def register(session: ConnectorSession) -> "Optional[ConnectorSession]":
    """Attach a connector for a user, returning any session it displaced.

    One connector per user: pairing a second machine and starting it should
    take over, not silently do nothing, and a reconnect after a dropped socket
    must not be refused by the corpse of the old one. The caller closes the
    returned session's socket.
    """
    with _lock:
        previous = _sessions.get(session.user_id)
        _sessions[session.user_id] = session
    # Closing happens outside the lock deliberately: close() schedules work on
    # the event loop and takes the displaced peer's own locks, and no registry
    # reader should be made to wait behind a socket teardown to find out who
    # its connector is.
    if previous is not None:
        # A distinct code, so the displaced client stops instead of
        # reconnecting and evicting this one straight back - see WS_REPLACED.
        previous.close("replaced by a newer connector", code=WS_REPLACED)
        _log.info(
            "connector for user %s replaced (%s -> %s)",
            session.user_id, previous.label, session.label,
        )
    return previous


def unregister(session: ConnectorSession) -> None:
    """Detach a connector, if it is still the registered one.

    The identity check matters: a slow disconnect of an already-displaced
    session must not evict the session that replaced it.
    """
    with _lock:
        if _sessions.get(session.user_id) is session:
            del _sessions[session.user_id]
    session.close()


def get(user_id: Optional[int]) -> "Optional[ConnectorSession]":
    if user_id is None:
        return None
    with _lock:
        session = _sessions.get(user_id)
    return None if session is not None and session.closed else session


def require(user_id: Optional[int]) -> ConnectorSession:
    """The user's connector, or raise the error callers know how to degrade on."""
    session = get(user_id)
    if session is None:
        raise ConnectorUnavailable(
            "No connector is attached. Start the wattracker connector on the "
            "machine where Zwift is installed."
        )
    return session


def is_attached(user_id: Optional[int]) -> bool:
    return get(user_id) is not None


def reset() -> None:
    """Drop every session. For tests and shutdown."""
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close("server shutting down")
