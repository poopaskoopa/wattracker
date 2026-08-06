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


class BleState:
    """The one live BLE connection this connector is holding, if any."""

    def __init__(self, buffer: Optional[RideBuffer] = None) -> None:
        self.conn: Optional[dict] = None
        self.sampler: Optional[asyncio.Task] = None
        # Every sample is written here as well as sent, so a dropped link -
        # or a killed process - costs seconds of a ride rather than all of it.
        self.buffer = buffer or RideBuffer()

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
        state.sampler = asyncio.create_task(_sample_loop())
        return {"devices": _describe(conn), "errors": conn.get("errors", [])}

    async def ble_set_erg(
        enabled: bool = False, watts: Optional[int] = None
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
                else:
                    # enable_erg(target) rather than a bare set: after a pause
                    # some trainers drop out of ERG entirely, and re-arming is
                    # what brings the resistance back.
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

    async def ble_release() -> dict:
        """End the session and hand the radio back to the OS.

        The ride page reconnects by stopping, waiting for the socket to close
        and reopening after a delay, precisely so the adapter is free before
        the next scan. With a network hop in the middle that delay is no longer
        enough on its own, so the server asks explicitly and waits.
        """
        await state.teardown()
        return {"released": True}

    async def _sample_loop() -> None:
        """One frame a second: the whole upstream data plane for a ride."""
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
                state.buffer.append(power=power, cadence=cadence, hr=hr)
                try:
                    await send_event(
                        "ble.sample", power=power, cadence=cadence, hr=hr
                    )
                except Exception:
                    # The socket died mid-ride. Keep sampling and recording -
                    # the rider is still pedalling, and this is exactly the
                    # case the buffer exists for. run_forever reconnects and
                    # uploads what was missed.
                    log.warning(
                        "lost the server mid-ride; still recording locally"
                    )
                await asyncio.sleep(SAMPLE_INTERVAL_S)
        except asyncio.CancelledError:
            raise

    return {
        "ble.available": ble_available,
        "ble.scan": ble_scan,
        "ble.connect": ble_connect,
        "ble.set_erg": ble_set_erg,
        "ble.disconnect": ble_disconnect,
        "ble.release": ble_release,
    }
