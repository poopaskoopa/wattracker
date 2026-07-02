"""Device abstractions + simulated devices + optional bleak-backed devices.

The abstract interfaces and simulated devices are pure Python (no hardware).
The bleak-backed classes are imported lazily and only used when ``bleak`` is
installed and a Bluetooth adapter is present.
"""
from __future__ import annotations

import abc
from typing import List, Optional, Sequence, Tuple

from .protocol import (
    cadence_from_cranks,
    encode_request_control,
    encode_set_target_power,
    encode_start,
    parse_cycling_power_measurement,
    parse_heart_rate_measurement,
    CYCLING_POWER_MEASUREMENT,
    FITNESS_MACHINE_CONTROL_POINT,
    HEART_RATE_MEASUREMENT,
)


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
    """Records every ERG target it is told to hold (for assertions in tests)."""

    def __init__(self) -> None:
        self.targets: List[int] = []

    def set_target_power(self, watts: int) -> None:
        self.targets.append(int(round(watts)))

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
    """FTMS trainer over bleak: request control, start, then set ERG target."""

    def __init__(self, client) -> None:
        self._client = client

    async def prepare(self) -> None:
        await self._client.write_gatt_char(
            FITNESS_MACHINE_CONTROL_POINT, encode_request_control(), response=True
        )
        await self._client.write_gatt_char(
            FITNESS_MACHINE_CONTROL_POINT, encode_start(), response=True
        )

    async def async_set_target_power(self, watts: int) -> None:
        await self._client.write_gatt_char(
            FITNESS_MACHINE_CONTROL_POINT, encode_set_target_power(watts), response=True
        )

    def set_target_power(self, watts: int) -> None:
        # Synchronous entry point: schedule the async write if an event loop is
        # running; otherwise run it to completion.
        import asyncio

        coro = self.async_set_target_power(watts)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)


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
