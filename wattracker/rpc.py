"""The wire protocol between the server and a connector.

The connector runs on the machine where Zwift lives, behind a home router with
no inbound ports, so it *dials out* to the server and holds one long-lived
WebSocket. Everything travels over that single socket:

    server -> connector   {"id": 17, "method": "activities.list", "params": {}}
    connector -> server   {"id": 17, "result": [...]}
                          {"id": 17, "error": "Folder not found"}
    connector -> server   {"event": "ble.sample", "power": 214, ...}

Requests only ever flow server -> connector: the server is the one with the
database and therefore the one that decides what needs doing. Events flow the
other way and are fire-and-forget - they carry live ride telemetry, where a
dropped frame matters far less than a stalled queue.

This module is deliberately transport-agnostic and dependency-free (stdlib
only, no fastapi, no bleak). Both ends import it, and the connector half must
stay light enough to freeze into a small executable.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Awaitable, Callable, Dict, Optional

# Wire-format version. Bumped when a change is not backward compatible; the
# server refuses a connector that does not match, because a silently
# half-understood protocol is worse than a refused connection.
#
# 2: a ride survives losing the socket. Adds ble.catchup, a force_rearm
#    parameter on ble.set_erg, a discard_buffer parameter on ble.release, and
#    an index on ble.sample. An older connector would answer "bad params" to
#    the ERG call every second of a ride, so this is a refusal case rather
#    than a degradation.
PROTOCOL_VERSION = 2

# Ceiling on a single frame. Activity files travel base64-encoded over this
# socket, and MAX_UPLOAD_BYTES is 50 MiB, so the limit has to clear
# 50 MiB * 4/3 plus envelope. Anything larger is a bug or an attack.
MAX_FRAME_BYTES = 72 * 1024 * 1024

# How long a request waits before giving up. A BLE scan legitimately takes
# seconds and a large .fit transfer can too, so this is generous; callers that
# know better pass their own.
DEFAULT_TIMEOUT_S = 60.0

# Close code for "another connector took over this account". In the private
# 4000-4999 range, so it can never collide with a protocol-level code.
#
# It exists because reconnecting is the wrong response to it, and the client
# cannot tell that from a normal close. Two connectors running for one account
# otherwise displace each other forever - each one reconnects, evicts the
# other, and gets evicted right back - which is a reconnect storm that looks
# from either side like a flaky network rather than the configuration mistake
# it is.
WS_REPLACED = 4409


class RpcError(RuntimeError):
    """The far end reported that it could not do what was asked."""


class ConnectorUnavailable(RuntimeError):
    """No connector is attached, or the one that was attached went away.

    Distinct from RpcError on purpose: this one means "the machine is not
    reachable", which callers degrade on (show a message, skip a maintenance
    stage), whereas RpcError means "it answered, and the answer was no".
    """


class ProtocolError(RpcError):
    """A frame did not conform to the protocol."""


def encode(message: dict) -> str:
    """Serialise one frame, refusing anything oversized."""
    text = json.dumps(message, separators=(",", ":"))
    if len(text) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame of {len(text)} bytes exceeds the {MAX_FRAME_BYTES} limit"
        )
    return text


def decode(raw: object) -> dict:
    """Parse one frame, rejecting anything that is not a JSON object.

    Bounds the input *before* parsing: a hostile peer must not be able to make
    either end allocate an arbitrary amount by sending one enormous frame.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise ProtocolError("frame must be text")
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(raw)} bytes exceeds the limit")
    try:
        message = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("frame must be a JSON object")
    return message


class RpcPeer:
    """One end of the request/response conversation over a WebSocket.

    Wraps any object exposing awaitable ``send_text(str)`` and
    ``receive_text() -> str``, which is what both Starlette's ``WebSocket`` and
    the connector's client adapter provide.

    The server side uses ``call``; the connector side uses ``serve``. Both
    share the framing so there is exactly one definition of the protocol.
    """

    def __init__(self, socket, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._socket = socket
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------- sending
    async def _send(self, message: dict) -> None:
        if self._closed:
            raise ConnectorUnavailable("connector connection is closed")
        # One writer at a time: concurrent ride telemetry and a file transfer
        # would otherwise interleave partial frames on the same socket.
        async with self._send_lock:
            await self._socket.send_text(encode(message))

    async def call(
        self, method: str, params: Optional[dict] = None,
        *, timeout: Optional[float] = None,
    ) -> Any:
        """Ask the far end to do something and wait for its answer."""
        if self._closed:
            raise ConnectorUnavailable("connector connection is closed")
        request_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"id": request_id, "method": method, "params": params or {}}
            )
            return await asyncio.wait_for(
                future, timeout if timeout is not None else self._timeout
            )
        except asyncio.TimeoutError as exc:
            raise ConnectorUnavailable(
                f"connector did not answer {method} within the timeout"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def send_event(self, event: str, **fields) -> None:
        """Fire-and-forget notification; no reply is expected or waited for."""
        await self._send({"event": event, **fields})

    # ------------------------------------------------------------ receiving
    def resolve(self, message: dict) -> bool:
        """Match an inbound frame to a waiting ``call``. True if it was one.

        Returns False for anything that is not a response - an event, or a
        request when this peer is the one being served - so the caller can
        route it onward.
        """
        request_id = message.get("id")
        if request_id is None or "method" in message:
            return False
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            # A late answer to a call that already timed out. Dropping it is
            # correct: the caller has long since been told it failed.
            return True
        if "error" in message:
            future.set_exception(RpcError(str(message.get("error"))))
        else:
            future.set_result(message.get("result"))
        return True

    async def serve(
        self,
        message: dict,
        handlers: Dict[str, Callable[..., Awaitable[Any]]],
    ) -> None:
        """Execute one inbound request and reply with its result or error.

        Every failure becomes an ``error`` response rather than propagating,
        because a handler raising must not take down the whole connection -
        the far end is waiting on this id and would otherwise hang until its
        timeout.
        """
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            await self._send({"id": request_id, "error": "params must be an object"})
            return
        handler = handlers.get(method)
        if handler is None:
            await self._send({"id": request_id, "error": f"unknown method: {method}"})
            return
        try:
            result = await handler(**params)
        except TypeError as exc:
            # Wrong arguments for the handler - a protocol mismatch, not a
            # crash. Report it as such rather than dropping the connection.
            await self._send({"id": request_id, "error": f"bad params: {exc}"})
        except Exception as exc:
            await self._send({"id": request_id, "error": str(exc) or type(exc).__name__})
        else:
            await self._send({"id": request_id, "result": result})

    # --------------------------------------------------------------- close
    def abandon(self, reason: str = "connector disconnected") -> None:
        """Fail every in-flight call. Called when the socket goes away.

        Without this, a thread blocked in ``RemoteBackend`` would sit on its
        full timeout after a disconnect it could have been told about at once.
        """
        self._closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectorUnavailable(reason))
        self._pending.clear()
