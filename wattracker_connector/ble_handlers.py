"""BLE, on the machine that actually has the radio.

The server owns the workout and the state machine; this end owns the hardware.
So the split is deliberately lopsided: everything here is either "talk to a
device" or "tell the server what the device said". No decisions.

While a ride is connected this pushes one ``ble.sample`` event per second -
power, cadence, heart rate - which is the entire upstream data plane. The
server's ``RideController`` consumes those three scalars exactly as it would
from a local radio.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable, Dict, List, Optional

from wattracker.ble import devices as bledevices

from .buffer import RideBuffer

log = logging.getLogger(__name__)

# Matches the server's RIDE_POLL_INTERVAL_S. Sampling faster would just send
# frames the controller throws away; slower and its 3s staleness window starts
# biting during normal operation.
SAMPLE_INTERVAL_S = 1.0

# How long a ride nobody is driving may sit at zero power before this end ends
# it on its own. Matches the server's RIDE_INACTIVITY_TIMEOUT_S, and exists for
# the case holding the trainer across a reconnect would otherwise create: a
# server that never comes back at all. While the rider keeps pedalling the ride
# keeps recording however long the outage runs - it is stopping that ends it,
# which is the same rule they are used to.
UNATTENDED_IDLE_S = 300.0


class BleState:
    """The one live BLE connection this connector is holding, if any."""

    def __init__(self, buffer: Optional[RideBuffer] = None) -> None:
        self.conn: Optional[dict] = None
        self.sampler: Optional[asyncio.Task] = None
        # Every sample is written here as well as sent, so a dropped link -
        # or a killed process - costs seconds of a ride rather than all of it.
        self.buffer = buffer or RideBuffer()
        # The ride the server started and has not yet ended, if any. It
        # deliberately outlives the socket: see ``detach``.
        self.ride: Optional[dict] = None
        # Whether a server is currently driving this ride. False from the
        # moment the socket drops until one asks for the samples it missed.
        self.claimed = False

    @property
    def riding(self) -> bool:
        return self.ride is not None

    async def detach(self) -> bool:
        """The socket went away. Release the radio only if no ride is on.

        This is where two requirements meet that used to be settled silently
        in favour of the wrong one. The radio must not be left held across a
        reconnect, because the server has no session to resume into and a
        half-held adapter is what stops the next scan finding anything - so
        the socket's ``finally`` tore everything down. But a rider mid-workout
        loses the whole session to a wifi stutter that way: FTMS Stop, both
        devices dropped, power 0.

        The reconciliation is that the first requirement is really about
        *scanning*, and both ``ble.scan`` and ``ble.connect`` already tear down
        before they do anything. So the adapter is freed exactly when it is
        needed, and a ride in progress keeps its devices, its sampler and its
        buffer. Returns True if the radio was released.
        """
        self.claimed = False
        if self.riding:
            log.warning(
                "lost the server mid-ride; holding the trainer and recording "
                "locally (%d samples so far)", self.buffer.count,
            )
            return False
        await self.teardown()
        return True

    async def end_ride(self) -> None:
        """Finish a ride for good: release the radio and drop the buffer.

        The buffer is only worth keeping while somebody still might need to be
        told about the ride. Once the server has ended it, the server has it -
        and leaving the file behind means the next reconnect uploads a ride
        that is already stored. It would not even dedupe: the hash is over
        (start, duration), and a controller's duration excludes the seconds
        the rider was paused, so the same ride would land as a second row.
        """
        await self.teardown()
        self.buffer.discard()

    async def teardown(self) -> None:
        """Release the radio completely. Safe to call when nothing is held.

        Order matters: stop sampling first, then stop the trainer, then drop
        the connections. A sampler still polling a half-disconnected device
        logs a great deal of noise for no purpose.
        """
        if self.sampler is not None:
            self.sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.sampler
            self.sampler = None
        self.buffer.finish()
        self.ride = None
        self.claimed = False
        conn, self.conn = self.conn, None
        if conn is None:
            return
        trainer = conn.get("trainer")
        if trainer is not None:
            # A fallback chain, not a sequence: the first name this trainer
            # actually offers is the one that releases it, so we stop there.
            # Order is the whole point. async_disable_erg and async_stop both
            # send FTMS stop and clear _erg_enabled; async_set_target_power(0)
            # does neither - it leaves ERG *engaged*, holding the wheel at 0 W.
            # Trying it first (as this once did) meant a real Trainer, which
            # defines all three, never got a release command at all. Same order
            # as devices.disconnect_sensor, for the same reason.
            for name, args in (
                ("async_disable_erg", ()), ("async_stop", ()),
                ("async_set_target_power", (0,)),
            ):
                method = getattr(trainer, name, None)
                if callable(method):
                    with contextlib.suppress(Exception):
                        await method(*args)
                    break
        for client in conn.get("clients", []) or []:
            with contextlib.suppress(Exception):
                await client.disconnect()


def _describe(conn: dict) -> List[dict]:
    """The connection as {address, name, roles} rows.

    Explicit pairs, unlike devices.connect_sensors' ``names`` map, which holds
    advertised names when auto-discovering and bare addresses when given a
    selection - so the browser cannot tell which it is looking at. Sending both
    every time removes the guess.
    """
    out: List[dict] = []
    for address, binding in (conn.get("bindings") or {}).items():
        out.append({
            "address": address,
            "name": binding.get("name") or address,
            "roles": sorted((binding.get("roles") or {}).keys()),
        })
    return out


def build_ble_handlers(
    state: BleState, send_event: Callable
) -> Dict[str, Callable]:
    """RPC methods for the BLE half. ``send_event`` pushes to the server."""

    async def ble_available() -> dict:
        available, reason = bledevices.bluetooth_available()
        return {"available": bool(available), "reason": reason}

    async def ble_scan(timeout: float = 5.0, attempts: int = 2) -> List[dict]:
        # A scan needs the radio to itself; anything still connected from a
        # previous ride will starve or corrupt it.
        await state.teardown()
        return await bledevices.scan(timeout=timeout, attempts=attempts)

    async def ble_connect(
        timeout: float = 6.0,
        selected: Optional[dict] = None,
        started_at: Optional[str] = None,
        name: str = "Ride",
        ftp: float = 0.0,
        workout_id: Optional[int] = None,
    ) -> dict:
        """Connect the sensors and begin recording.

        The server sends the ride's identity along with the request - start
        time, name, FTP, plan workout - so that if the link dies mid-ride this
        end can still describe what it recorded. Asking for it afterwards
        would mean asking exactly when there is nobody to ask.
        """
        await state.teardown()
        conn = (
            await bledevices.connect_sensors(timeout=timeout)
            if selected is None
            else await bledevices.connect_sensors(timeout=timeout, selected=selected)
        )
        state.conn = conn
        if started_at:
            state.buffer.start(started_at, name, ftp, workout_id)
            state.ride = {
                "started_at": started_at, "name": name, "ftp": float(ftp),
                "workout_id": workout_id,
            }
            state.claimed = True
        state.sampler = asyncio.create_task(_sample_loop())
        return {"devices": _describe(conn), "errors": conn.get("errors", [])}

    async def ble_catchup(since: int = 0) -> dict:
        """The samples recorded from index ``since``, and take the ride back.

        Answering this is what marks the ride claimed: a server that asks for
        the missing seconds is a server that intends to keep driving. Until
        one does, the client's claim watchdog is counting down on the
        assumption that nobody is coming.

        ``active`` is the part a returning server cannot work out for itself.
        The ride it left may no longer exist - this end ends one of its own
        accord if the rider stops for long enough with nobody driving - and
        the samples still come back either way, because they are worth having
        regardless of whether there is a ride left to carry on.
        """
        state.claimed = True
        rows = state.buffer.samples_from(since)
        return {
            "since": int(since),
            "count": state.buffer.count,
            "active": state.riding,
            "samples": rows,
        }

    async def ble_set_erg(
        enabled: bool = False, watts: Optional[int] = None,
        force_rearm: bool = False,
    ) -> dict:
        conn = state.conn or {}
        trainer = conn.get("trainer")
        if trainer is None:
            return {"available": False, "enabled": False,
                    "error": "No FTMS trainer is connected."}
        try:
            if enabled:
                if watts is None:
                    await trainer.async_enable_erg()
                elif force_rearm or not getattr(trainer, "erg_enabled", False):
                    # enable_erg(target) rather than a bare set: after a pause
                    # some trainers drop out of ERG entirely, and re-arming is
                    # what brings the resistance back. The server already knows
                    # when that applies - it is the same distinction
                    # _set_connection_erg draws locally - so it says so rather
                    # than this end guessing, which used to mean three FTMS
                    # writes (0x00, 0x07, 0x05) on every 1 Hz tick where local
                    # mode issued one.
                    await trainer.async_enable_erg(int(watts))
                else:
                    set_target = getattr(trainer, "async_set_target_power", None)
                    if callable(set_target):
                        await set_target(int(watts))
                    else:
                        await trainer.async_enable_erg(int(watts))
            else:
                await trainer.async_stop()
        except Exception as exc:
            return {
                "available": bool(getattr(trainer, "erg_available", True)),
                "enabled": False,
                "error": str(exc),
            }
        return {
            "available": bool(getattr(trainer, "erg_available", True)),
            "enabled": bool(getattr(trainer, "erg_enabled", enabled)),
            "error": None,
        }

    async def ble_disconnect(address: str = "") -> dict:
        conn = state.conn
        if conn is None:
            raise ValueError("Nothing is connected.")
        await bledevices.disconnect_sensor(conn, address)
        return {"devices": _describe(conn)}

    async def ble_release(discard_buffer: bool = True) -> dict:
        """End the session and hand the radio back to the OS.

        The ride page reconnects by stopping, waiting for the socket to close
        and reopening after a delay, precisely so the adapter is free before
        the next scan. With a network hop in the middle that delay is no longer
        enough on its own, so the server asks explicitly and waits.

        This is also where the buffer is dropped, because reaching it normally
        means the server is here and has recorded the ride itself.
        ``discard_buffer=False`` is how it says otherwise: a ride it gave up on
        while we were unreachable is *ours* to upload, and discarding it here
        because the link happened to come back first would lose it outright.
        """
        if discard_buffer:
            await state.end_ride()
        else:
            await state.teardown()
        return {"released": True}

    async def _sample_loop() -> None:
        """One frame a second: the whole upstream data plane for a ride."""
        offline_frames = 0
        idle_frames = 0
        try:
            while True:
                conn = state.conn
                if conn is None:
                    return
                power_source = conn.get("power_source")
                hr_source = conn.get("hr_source")
                power = power_source.latest_power() if power_source else None
                cadence = power_source.latest_cadence() if power_source else None
                hr = hr_source.latest_hr() if hr_source else None
                # Recorded before it is sent, deliberately: if the send is what
                # fails, the sample is already safe on disk.
                index = state.buffer.append(
                    power=power, cadence=cadence, hr=hr
                )
                try:
                    await send_event(
                        "ble.sample", power=power, cadence=cadence, hr=hr,
                        n=index,
                    )
                    offline_frames = 0
                except Exception:
                    # The socket died mid-ride. Keep sampling and recording -
                    # the rider is still pedalling, and this is exactly the
                    # case the buffer exists for. run_forever reconnects and
                    # the server replays what it missed from index ``n``.
                    #
                    # Said once, not once a second: an outage long enough to
                    # matter would otherwise bury its own diagnosis under
                    # thousands of identical lines.
                    if offline_frames == 0:
                        log.warning(
                            "lost the server mid-ride; still recording locally"
                        )
                    offline_frames += 1
                if state.claimed or power:
                    idle_frames = 0
                else:
                    idle_frames += 1
                    if idle_frames * SAMPLE_INTERVAL_S >= UNATTENDED_IDLE_S:
                        log.warning(
                            "no server and no power for %.0fs; ending the ride "
                            "and releasing the trainer", UNATTENDED_IDLE_S,
                        )
                        # Not awaited: teardown cancels this very task, and a
                        # task awaiting its own cancellation never returns.
                        asyncio.create_task(state.teardown())
                        return
                await asyncio.sleep(SAMPLE_INTERVAL_S)
        except asyncio.CancelledError:
            raise

    return {
        "ble.available": ble_available,
        "ble.scan": ble_scan,
        "ble.connect": ble_connect,
        "ble.catchup": ble_catchup,
        "ble.set_erg": ble_set_erg,
        "ble.disconnect": ble_disconnect,
        "ble.release": ble_release,
    }
