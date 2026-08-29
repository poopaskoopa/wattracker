"""Holds the connection to the server and answers what it asks.

The connector dials out and keeps one WebSocket open. When it drops - server
restart, laptop sleep, flaky wifi - it reconnects with exponential backoff and
carries on. There is nothing to resynchronise on reconnect: the server drives
every exchange, and its own ``scanned_files`` cache means the next scan picks
up exactly where the last one stopped.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import random
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from wattracker import rpc

from . import watcher
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

# A much shorter ceiling while a ride is in progress. A minute of backoff is a
# sensible way to wait out an overnight outage and a terrible way to wait out
# one during a workout: on real hardware a 30-second drop cost 2m 06s of
# reconnect, because the delay had already climbed to 56 s by the time the link
# returned. Every second of that is a second the trainer holds a stale target.
_RIDE_BACKOFF_MAX_S = 5.0

# How long a reconnected connector holding a ride waits for a server to ask
# for the samples it missed. Past this nobody is coming - the browser closed,
# or the server gave up on us first - and continuing to hold the trainer in
# ERG would leave a rider pushing against a workout that has already ended.
CLAIM_TIMEOUT_S = 90.0

# Sent by the server as soon as it accepts. Its absence within this many
# seconds means we are talking to something that is not a wattracker server.
_HELLO_TIMEOUT_S = 20.0


async def _cancel(task: "Optional[asyncio.Task]") -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # Not the cancellation we asked for: this task died on its own, before
        # anyone came to stop it. Swallowing it silently is how a crashed
        # folder watcher becomes a connector that looks healthy and never
        # reports another ride - the exact failure this module exists to
        # prevent - so it is said out loud even though shutdown continues.
        log.warning("a background connector task failed", exc_info=True)


class _Replaced(Exception):
    """The server closed us because another connector took over the account."""


class ConnectorStatus:
    """What the tray icon shows. Plain attributes, read from another thread."""

    def __init__(self) -> None:
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_connected_at: Optional[str] = None
        self.server_url: Optional[str] = None
        # Set once ``run_forever`` has returned, which it only does when
        # nothing further will be attempted. The distinction the tray draws is
        # between "not connected, trying" and "not connected, and that is the
        # end of it" - two states that look identical from ``connected`` alone
        # and want very different icons.
        self.stopped = False
        # Filled in only when stopping was not the rider's own doing, with
        # something they can act on. None after a quit.
        self.stopped_reason: Optional[str] = None


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
        scan_interval: Optional[float] = None,
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
        # Watches the Zwift folder so a finished ride reaches the server in
        # about a minute instead of on the server's daily sweep. The interval
        # is settable (and 0 turns it off) because it is the one thing here a
        # rider might reasonably want to trade: promptness against a folder
        # read they can feel on a slow or spun-down disk.
        self.scan_interval = watcher.normalize_interval(scan_interval)
        self._watcher = watcher.ActivityWatcher(config)
        # Set when the folder has changed and the server has not been told
        # yet. Survives a disconnect: the change happened whether or not there
        # was a socket to report it on, and the flush is the second thing the
        # next connection does, right behind the buffered ride.
        #
        # Deliberately weaker than the buffered ride, and worth being precise
        # about because the two sit one line apart. The ride is a JSONL file on
        # disk, discarded only on a definite HTTP answer, so it survives this
        # process dying. This flag is a bool in memory, cleared on a
        # fire-and-forget send_event that nothing acknowledges. So it
        # guarantees exactly one thing: news noticed while the socket was down
        # is not forgotten while this process lives. A frame that reaches the
        # socket but not the server clears the flag anyway, and the watcher's
        # _reported already holds that file, so it is never raised again - the
        # ride then waits for a restart, a Rescan, or the daily sweep. That is
        # the bargain the event protocol makes everywhere (see
        # _handle_connector_event), with the sweep as the backstop.
        self._activities_dirty = False
        # Strong references to the in-flight request tasks. The loop holds
        # only weak ones, so a task nothing else refers to may be collected
        # mid-await - see _serve.
        self._serving: "set[asyncio.Task]" = set()

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
        # Started here, not inside a session: the folder has to keep
        # being watched while the socket is down, or a ride finished
        # during a server restart goes unnoticed until the next one.
        watch = self._start_activity_watch()
        try:
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
                    # The close frame's own text says "4409" and nothing a rider
                    # can use, and the tray has no log to read - so the sentence
                    # that explains this is put where the tray will find it.
                    self.status.stopped_reason = (
                        "Another connector has taken this account over. Only one "
                        "connector may run per account: quit the other one, or "
                        "pair this machine as its own device."
                    )
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
                ceiling = _RIDE_BACKOFF_MAX_S if self.ble.riding else _BACKOFF_MAX_S
                delay = min(backoff, ceiling) * (0.5 + random.random())
                log.info("reconnecting in %.1fs", delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX_S)
        finally:
            await _cancel(watch)
        # Leaving the loop is definitive - Ctrl-C, the tray quitting, or being
        # displaced by another connector - not a reconnect. So the radio goes
        # back even if a ride was in progress: nothing is going to pick it up.
        # The buffer survives, and goes up the next time this starts.
        await self.ble.teardown()
        # Definitive is exactly what the tray needs to know: until this, a
        # disconnected connector is one that is still trying.
        self.status.stopped = True

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
                # Local time, and a string rather than a timestamp: its only
                # consumer is a tray menu line, and formatting it here keeps
                # the reader on the other thread doing nothing but reading.
                self.status.last_connected_at = datetime.datetime.now().isoformat(
                    timespec="seconds"
                )
                log.info("connected")
                # First thing after every connect, before serving anything: a
                # ride buffered while we were away is the most perishable
                # thing this process holds, and there is no reason to make it
                # wait behind a scan.
                await self._flush_buffered_ride()
                # Then anything the folder gained while we were away. A ride
                # ridden during a server restart is noticed by the watcher at
                # the time and reported here, which is what makes a connector
                # start (or a reconnect) the cold-start trigger for a scan.
                await self._flush_activity_signal()
                claim = self._start_claim_watchdog()
                try:
                    await self._serve(socket, peer)
                finally:
                    await _cancel(claim)
            finally:
                self._peer = None
                self.status.connected = False
                peer.abandon("disconnected")
                # Releases the radio, unless a ride is in progress - in which
                # case the devices, the sampler and the buffer all outlive the
                # socket, and the ride is picked up again on reconnect. See
                # BleState.detach for why that is not a contradiction of the
                # rule about never holding the adapter across a reconnect.
                await self.ble.detach()

    def _start_claim_watchdog(self) -> Optional[asyncio.Task]:
        """After a reconnect mid-ride, make sure somebody takes the ride back."""
        if not self.ble.riding:
            return None
        log.info(
            "reconnected while riding; waiting for the server to pick the "
            "ride back up"
        )
        return asyncio.create_task(self._abandon_unclaimed_ride())

    async def _abandon_unclaimed_ride(self) -> None:
        """End a ride no returning server has claimed. The safety net.

        Without it, "keep the trainer through a reconnect" turns into "hold
        the trainer forever" the moment the other end is not coming back - a
        closed ride page, or a server that timed the ride out while we were
        away. The rider would be left pushing against a workout nobody is
        running, with no page to stop it from.
        """
        await asyncio.sleep(CLAIM_TIMEOUT_S)
        if not self.ble.riding or self.ble.claimed:
            return
        log.warning(
            "no server picked the ride up within %.0fs; releasing the trainer "
            "and uploading what was recorded", CLAIM_TIMEOUT_S,
        )
        await self.ble.teardown()
        await self._flush_buffered_ride()

    async def _flush_buffered_ride(self) -> None:
        """Upload a ride recorded while the server was unreachable."""
        if self.ble.riding:
            # The ride is still being ridden. Uploading now would store half a
            # workout as a finished activity, and - because the buffer is
            # discarded on a successful upload - throw away the half still to
            # come. It goes up when the ride actually ends.
            return
        try:
            await asyncio.to_thread(
                upload_pending, self.server_url, self.token, self.ble.buffer
            )
        except Exception:
            # Never let this stop the session starting - the buffer keeps the
            # ride and the next reconnect tries again.
            log.warning("could not upload the buffered ride", exc_info=True)

    # ------------------------------------------------- watching the folder
    def _start_activity_watch(self) -> Optional[asyncio.Task]:
        """The folder-watching task, or None if the rider turned it off."""
        if self.scan_interval <= 0:
            log.info(
                "not watching the Zwift folder (scan interval is 0); the "
                "server's own sweep is what will pick rides up"
            )
            return None
        log.info(
            "watching %s every %.0fs",
            ", ".join(self._watcher.folders()) or "(no Zwift folder yet)",
            self.scan_interval,
        )
        return asyncio.create_task(self._watch_activities())

    async def _watch_activities(self) -> None:
        """Poll the Activities folders forever, reporting what settles.

        Deliberately unkillable by its own failures: a folder that cannot be
        read is a reason to try again next minute, not a reason to stop
        watching for the rest of the session.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.scan_interval
                )
                return  # stopping
            except asyncio.TimeoutError:
                pass
            try:
                # os.scandir on a spun-down or networked disk can block for
                # long enough to matter, and this loop shares the event loop
                # with a ride's 1 Hz telemetry.
                if await asyncio.to_thread(self._watcher.poll):
                    self._activities_dirty = True
            except Exception:
                log.warning("could not check the Zwift folder", exc_info=True)
            await self._flush_activity_signal()

    async def _flush_activity_signal(self) -> None:
        """Tell the server the folder changed, if it changed and we can.

        The flag is cleared only on a successful send. An event is
        fire-and-forget with no acknowledgement, so this is the one chance to
        notice the socket was gone - and holding the flag means the news goes
        out on the next connection rather than waiting for the next ride to
        overwrite it.
        """
        if not self._activities_dirty:
            return
        try:
            await self._send_event("activities.changed")
        except Exception:
            # Includes the ordinary "not connected": nothing to do but keep
            # the flag. Debug rather than warning - an offline connector
            # noticing a ride is expected, not a fault.
            log.debug("could not report the folder change yet", exc_info=True)
            return
        self._activities_dirty = False

    async def _serve(self, socket, peer: rpc.RpcPeer) -> None:
        """Answer requests until the socket closes or we are told to stop."""
        while not self._stop.is_set():
            message = rpc.decode(await socket.receive_text())
            if peer.resolve(message):
                continue
            if "method" in message:
                # Served as a task so a slow call (a big file read, a BLE
                # scan) does not stall the ones behind it.
                #
                # Kept in a set until it finishes, because asyncio holds only
                # a weak reference to a running task: one that nothing else
                # refers to can be garbage collected part-way through, and
                # what the server sees is a request that is never answered -
                # it waits out its own timeout instead. Nothing here reads the
                # set; existing is its whole job. Exceptions are not the
                # reason - rpc.serve turns every one of them into an error
                # response by contract, so there is nothing to retrieve.
                task = asyncio.create_task(peer.serve(message, self._handlers))
                self._serving.add(task)
                task.add_done_callback(self._serving.discard)
