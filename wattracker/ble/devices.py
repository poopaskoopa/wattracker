"""Device abstractions + simulated devices + optional bleak-backed devices.

The abstract interfaces and simulated devices are pure Python (no hardware).
The bleak-backed classes are imported lazily and only used when ``bleak`` is
installed and a Bluetooth adapter is present.
"""
from __future__ import annotations

import abc
import logging
from typing import List, Optional, Sequence, Tuple

from .protocol import (
    cadence_from_cranks,
    encode_request_control,
    encode_set_target_power,
    encode_start,
    encode_stop,
    parse_control_point_response,
    parse_cycling_power_measurement,
    parse_heart_rate_measurement,
    CYCLING_POWER_SERVICE,
    CYCLING_POWER_MEASUREMENT,
    FITNESS_MACHINE_SERVICE,
    FITNESS_MACHINE_CONTROL_POINT,
    HEART_RATE_SERVICE,
    HEART_RATE_MEASUREMENT,
)

log = logging.getLogger(__name__)


def bleak_available() -> Tuple[bool, str]:
    """Return (available, reason). Feature-detects the optional ``bleak`` import.

    Note: this only checks that the library imports. Whether an actual adapter
    is present is discovered at scan/connect time and surfaced there.
    """
    try:
        import bleak  # noqa: F401
    except Exception as e:  # ImportError or platform backend error
        return False, f"bleak not installed ({e.__class__.__name__})"
    return True, "bleak available"


# Backwards/clearer alias used by the web layer.
def bluetooth_available() -> Tuple[bool, str]:
    return bleak_available()


# --------------------------------------------------------------- interfaces
class PowerSource(abc.ABC):
    """A source of instantaneous power (W) and optional cadence (rpm)."""

    @abc.abstractmethod
    def latest_power(self) -> Optional[int]: ...

    def latest_cadence(self) -> Optional[float]:
        return None


class HeartRateSource(abc.ABC):
    @abc.abstractmethod
    def latest_hr(self) -> Optional[int]: ...


class Trainer(abc.ABC):
    """A controllable trainer supporting ERG (set target power)."""

    @abc.abstractmethod
    def set_target_power(self, watts: int) -> None: ...

    def start_erg(self) -> None:
        """Take control + start (FTMS Request Control 0x00, Start/Resume 0x07)."""

    def stop_erg(self) -> None:
        """Release ERG at ride end (FTMS Stop 0x08)."""


# --------------------------------------------------------------- simulated
class SimulatedPowerSource(PowerSource):
    """Replays a scripted sequence of (power, cadence) samples.

    Once the script is exhausted it holds the last value. Great for driving the
    RideController in tests with no hardware.
    """

    def __init__(
        self,
        powers: Sequence[int],
        cadences: Optional[Sequence[float]] = None,
    ):
        self._powers = list(powers)
        self._cadences = list(cadences) if cadences is not None else None
        self._i = -1

    def advance(self) -> None:
        self._i += 1

    def _clamp(self) -> int:
        if not self._powers:
            return 0
        return min(max(self._i, 0), len(self._powers) - 1)

    def latest_power(self) -> Optional[int]:
        if not self._powers:
            return None
        return self._powers[self._clamp()]

    def latest_cadence(self) -> Optional[float]:
        if self._cadences is None:
            return None
        return self._cadences[min(max(self._i, 0), len(self._cadences) - 1)]


class SimulatedHeartRateSource(HeartRateSource):
    def __init__(self, hrs: Optional[Sequence[int]] = None, fixed: int = 140):
        self._hrs = list(hrs) if hrs is not None else None
        self._fixed = fixed
        self._i = -1

    def advance(self) -> None:
        self._i += 1

    def latest_hr(self) -> Optional[int]:
        if self._hrs is None:
            return self._fixed
        return self._hrs[min(max(self._i, 0), len(self._hrs) - 1)]


class SimulatedTrainer(Trainer):
    """Records every ERG target/command it is told to hold (for test assertions)."""

    def __init__(self) -> None:
        self.targets: List[int] = []
        self.commands: List[str] = []

    def set_target_power(self, watts: int) -> None:
        self.targets.append(int(round(watts)))

    def start_erg(self) -> None:
        self.commands.append("request_control")
        self.commands.append("start")

    def stop_erg(self) -> None:
        self.commands.append("stop")

    @property
    def last_target(self) -> Optional[int]:
        return self.targets[-1] if self.targets else None


# --------------------------------------------------------- bleak-backed (opt)
class BleakPowerSource(PowerSource):
    """Cycling Power Service power/cadence via bleak notifications.

    Instantiating this requires ``bleak`` and a connected ``BleakClient``. It is
    never exercised in the no-hardware test suite.
    """

    def __init__(self, client) -> None:
        self._client = client
        self._power: Optional[int] = None
        self._cadence: Optional[float] = None
        self._prev_revs: Optional[int] = None
        self._prev_time: Optional[int] = None

    async def start(self) -> None:
        await self._client.start_notify(CYCLING_POWER_MEASUREMENT, self._on_notify)

    def _on_notify(self, _char, data: bytearray) -> None:
        parsed = parse_cycling_power_measurement(bytes(data))
        self._power = parsed["power"]
        revs, time = parsed["crank_revs"], parsed["crank_event_time"]
        if revs is not None and time is not None:
            cad = cadence_from_cranks(self._prev_revs, self._prev_time, revs, time)
            if cad is not None:
                self._cadence = cad
            self._prev_revs, self._prev_time = revs, time

    def latest_power(self) -> Optional[int]:
        return self._power

    def latest_cadence(self) -> Optional[float]:
        return self._cadence


class BleakHeartRateSource(HeartRateSource):
    def __init__(self, client) -> None:
        self._client = client
        self._hr: Optional[int] = None

    async def start(self) -> None:
        await self._client.start_notify(HEART_RATE_MEASUREMENT, self._on_notify)

    def _on_notify(self, _char, data: bytearray) -> None:
        self._hr = parse_heart_rate_measurement(bytes(data))["hr"]

    def latest_hr(self) -> Optional[int]:
        return self._hr


class BleakTrainer(Trainer):
    """FTMS trainer over bleak: request control, start, then set ERG targets.

    Control-point indications (response code 0x80) are logged: failures are
    reported but never raised, so a stubborn trainer degrades to display-only.
    """

    def __init__(self, client) -> None:
        self._client = client
        self.last_response: Optional[dict] = None

    def _on_control_point(self, _char, data: bytearray) -> None:
        try:
            resp = parse_control_point_response(bytes(data))
        except ValueError as e:
            log.debug("Ignoring non-response FTMS indication: %s", e)
            return
        self.last_response = resp
        if resp["success"]:
            log.debug("FTMS op 0x%02x acknowledged", resp["request_op"])
        else:
            log.warning(
                "FTMS op 0x%02x rejected: %s", resp["request_op"], resp["message"]
            )

    async def _write(self, payload: bytes, what: str) -> bool:
        try:
            await self._client.write_gatt_char(
                FITNESS_MACHINE_CONTROL_POINT, payload, response=True
            )
            return True
        except Exception as e:  # trainer went away / write rejected
            log.warning("FTMS %s write failed: %s", what, e)
            return False

    async def prepare(self) -> None:
        """Put the trainer in ERG: subscribe to responses, take control, start."""
        try:
            await self._client.start_notify(
                FITNESS_MACHINE_CONTROL_POINT, self._on_control_point
            )
        except Exception as e:  # indications unsupported: continue blind
            log.warning("FTMS control point indications unavailable: %s", e)
        await self._write(encode_request_control(), "request control")
        await self._write(encode_start(), "start")

    async def async_set_target_power(self, watts: int) -> None:
        await self._write(encode_set_target_power(watts), "set target power")

    async def async_stop(self) -> None:
        await self._write(encode_stop(), "stop")

    def _schedule(self, coro) -> None:
        # Synchronous entry point: schedule the async write if an event loop is
        # running; otherwise run it to completion.
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)

    def set_target_power(self, watts: int) -> None:
        self._schedule(self.async_set_target_power(watts))

    def start_erg(self) -> None:
        self._schedule(self.prepare())

    def stop_erg(self) -> None:
        self._schedule(self.async_stop())


async def scan(timeout: float = 5.0) -> List[dict]:
    """Discover nearby BLE devices grouped by advertised service.

    Requires ``bleak`` + an adapter. Raises RuntimeError when unavailable.
    """
    ok, reason = bleak_available()
    if not ok:
        raise RuntimeError(f"Bluetooth unavailable: {reason}")
    from bleak import BleakScanner  # type: ignore

    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out: List[dict] = []
    for _addr, (device, adv) in devices.items():
        out.append(
            {
                "address": device.address,
                "name": device.name or adv.local_name or "(unknown)",
                "services": list(adv.service_uuids or []),
                "rssi": adv.rssi,
            }
        )
    return out


async def connect_sensors(timeout: float = 6.0) -> dict:
    """Scan and connect the first FTMS trainer / power meter / HR strap found.

    Returns ``{"trainer", "power_source", "hr_source", "clients", "names"}``.
    Any of the three roles may be None (graceful degradation: e.g. power-only
    with no controllable trainer). The caller must disconnect every client in
    ``clients`` when the ride ends. Raises RuntimeError when bleak/adapter is
    unavailable.
    """
    ok, reason = bleak_available()
    if not ok:
        raise RuntimeError(f"Bluetooth unavailable: {reason}")
    from bleak import BleakClient  # type: ignore

    found = await scan(timeout=timeout)

    def _first_with(service_uuid: str) -> Optional[dict]:
        for d in found:
            if service_uuid in [s.lower() for s in d["services"]]:
                return d
        return None

    roles = {
        "trainer": _first_with(FITNESS_MACHINE_SERVICE),
        "power": _first_with(CYCLING_POWER_SERVICE),
        "hr": _first_with(HEART_RATE_SERVICE),
    }

    clients: dict = {}  # address -> connected BleakClient (dedup: one per device)
    out = {"trainer": None, "power_source": None, "hr_source": None,
           "clients": [], "names": {}}
    for role, dev in roles.items():
        if not dev:
            continue
        addr = dev["address"]
        client = clients.get(addr)
        if client is None:
            client = BleakClient(addr)
            try:
                await client.connect()
            except Exception as e:
                log.warning("Could not connect %s (%s): %s", dev["name"], addr, e)
                continue
            clients[addr] = client
            out["clients"].append(client)
        out["names"][role] = dev["name"]
        try:
            if role == "trainer":
                trainer = BleakTrainer(client)
                await trainer.prepare()
                out["trainer"] = trainer
            elif role == "power":
                src = BleakPowerSource(client)
                await src.start()
                out["power_source"] = src
            elif role == "hr":
                hr = BleakHeartRateSource(client)
                await hr.start()
                out["hr_source"] = hr
        except Exception as e:  # role setup failed: keep the others working
            log.warning("Could not set up %s on %s: %s", role, dev["name"], e)
    return out
