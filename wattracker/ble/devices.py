"""Device abstractions + simulated devices + optional bleak-backed devices.

The abstract interfaces and simulated devices are pure Python (no hardware).
The bleak-backed classes are imported lazily and only used when ``bleak`` is
installed and a Bluetooth adapter is present.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import time
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .protocol import (
    cadence_from_cranks,
    encode_request_control,
    encode_set_target_power,
    encode_start,
    encode_stop,
    parse_control_point_response,
    parse_csc_measurement,
    parse_cycling_power_measurement,
    parse_heart_rate_measurement,
    CYCLING_POWER_SERVICE,
    CYCLING_POWER_MEASUREMENT,
    CYCLING_SPEED_AND_CADENCE_SERVICE,
    CYCLING_SPEED_AND_CADENCE_MEASUREMENT,
    FITNESS_MACHINE_SERVICE,
    FITNESS_MACHINE_CONTROL_POINT,
    HEART_RATE_SERVICE,
    HEART_RATE_MEASUREMENT,
)

log = logging.getLogger(__name__)

BLE_VALUE_STALE_S = 3.0

# Bound each per-client BLE connect so a device the OS still holds (common with
# a KICKR right after a session) fails fast with a visible error instead of
# hanging the WebSocket forever.
CONNECT_TIMEOUT_S = 10.0
CONNECT_RETRY_DELAY_S = 0.25
SCAN_ATTEMPTS = 2


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


class CadenceSource(abc.ABC):
    """A source of cadence (rpm) without an implied power measurement."""

    @abc.abstractmethod
    def latest_cadence(self) -> Optional[float]: ...


class AggregatePowerSource(PowerSource):
    """Combine independent power meters into one rider power source.

    Dual-sided pedals that advertise as two devices report each pedal's watts
    independently. Available readings are summed; cadence is averaged across
    the sources currently reporting it (normally both pedals report the same
    crank cadence).
    """

    def __init__(self, sources: Sequence[PowerSource]) -> None:
        self.sources = list(sources)

    def latest_power(self) -> Optional[int]:
        readings = [source.latest_power() for source in self.sources]
        available = [watts for watts in readings if watts is not None]
        return sum(available) if available else None

    def latest_cadence(self) -> Optional[float]:
        readings = [source.latest_cadence() for source in self.sources]
        available = [cadence for cadence in readings if cadence is not None]
        return sum(available) / len(available) if available else None


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

    @property
    def erg_available(self) -> bool:
        return True

    @property
    def erg_enabled(self) -> bool:
        return False


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
        self._erg_enabled = False

    def set_target_power(self, watts: int) -> None:
        self.targets.append(int(round(watts)))

    def start_erg(self) -> None:
        self.commands.append("request_control")
        self.commands.append("start")
        self._erg_enabled = True

    def stop_erg(self) -> None:
        self.commands.append("stop")
        self._erg_enabled = False

    @property
    def erg_enabled(self) -> bool:
        return self._erg_enabled

    @property
    def last_target(self) -> Optional[int]:
        return self.targets[-1] if self.targets else None


# --------------------------------------------------------- bleak-backed (opt)
class _CrankCadenceTracker:
    """Derive rpm from cumulative crank revolutions + last crank event time.

    Shared by the Cycling Power (0x2A63) and CSC (0x2A5B) notification
    handlers so the two cannot drift apart.
    """

    def __init__(self) -> None:
        self.cadence: Optional[float] = None
        self.updated_at: Optional[float] = None
        self._prev_revs: Optional[int] = None
        self._prev_time: Optional[int] = None

    def update(self, revs: Optional[int], event_time: Optional[int]) -> None:
        if revs is None or event_time is None:
            return
        cad = cadence_from_cranks(self._prev_revs, self._prev_time, revs, event_time)
        if cad is not None:
            self.cadence = cad
            self.updated_at = time.monotonic()
        # A sensor may notify faster than new crank events occur. Repeated
        # event data is not a zero-rpm observation and must not refresh
        # cadence freshness.
        if self._prev_time is None or event_time != self._prev_time:
            self._prev_revs, self._prev_time = revs, event_time

    def latest(self, stale_after_s: float) -> Optional[float]:
        if (
            self.updated_at is None
            or time.monotonic() - self.updated_at > stale_after_s
        ):
            return None
        return self.cadence


def _client_characteristic_uuids(client) -> Optional[Set[str]]:
    """Every characteristic UUID the connected device exposes, lowercased.

    ``None`` when the client cannot be inspected (no resolved services).

    bleak 3.x *raises* from ``.services`` before service discovery has run
    rather than returning ``None``, so reading it has to be guarded too --
    an uninspectable client must fall through to probing, never propagate.
    """
    try:
        services = getattr(client, "services", None)
    except Exception:
        return None
    if services is None:
        return None
    try:
        uuids = {
            str(char.uuid).lower()
            for service in services
            for char in service.characteristics
        }
    except Exception:
        return None
    return uuids or None


class BleakPowerSource(PowerSource):
    """Cycling Power Service power/cadence via bleak notifications.

    Instantiating this requires ``bleak`` and a connected ``BleakClient``. It is
    never exercised in the no-hardware test suite.
    """

    # The characteristic this role's notifications carry cadence on, so a
    # cadence role bound to the same client can share it instead of
    # subscribing twice (see ``_shared_cadence_provider``).
    CADENCE_CHARACTERISTIC = CYCLING_POWER_MEASUREMENT

    def __init__(self, client, stale_after_s: float = BLE_VALUE_STALE_S) -> None:
        self._client = client
        self._stale_after_s = float(stale_after_s)
        self._power: Optional[int] = None
        self._power_updated_at: Optional[float] = None
        self._cranks = _CrankCadenceTracker()

    async def start(self) -> None:
        await self._client.start_notify(CYCLING_POWER_MEASUREMENT, self._on_notify)

    def _on_notify(self, _char, data: bytearray) -> None:
        parsed = parse_cycling_power_measurement(bytes(data))
        self._power = parsed["power"]
        self._power_updated_at = time.monotonic()
        self._cranks.update(parsed["crank_revs"], parsed["crank_event_time"])

    def latest_power(self) -> Optional[int]:
        if (
            self._power_updated_at is None
            or time.monotonic() - self._power_updated_at > self._stale_after_s
        ):
            return None
        return self._power

    def latest_cadence(self) -> Optional[float]:
        return self._cranks.latest(self._stale_after_s)


class BleakCadenceSource(CadenceSource, PowerSource):
    """Cadence via bleak notifications, with no power measurement.

    Prefers the CSC measurement (0x2A5B). Devices that report crank
    revolutions only inside the Cycling Power measurement (0x2A63) -- a Wahoo
    KICKR, for instance, exposes no CSC service at all -- are read from that
    characteristic instead. ``latest_power()`` stays ``None`` either way: this
    class is cadence-only by contract.

    It also implements ``PowerSource`` so legacy consumers can use a
    cadence-only connection through ``power_source``.
    """

    def __init__(self, client, stale_after_s: float = BLE_VALUE_STALE_S) -> None:
        self._client = client
        self._stale_after_s = float(stale_after_s)
        self._cranks = _CrankCadenceTracker()

    async def start(self) -> None:
        candidates = (
            (CYCLING_SPEED_AND_CADENCE_MEASUREMENT, self._on_notify),
            (CYCLING_POWER_MEASUREMENT, self._on_power_notify),
        )
        available = self._characteristic_uuids()
        if available is not None:
            for uuid, handler in candidates:
                if uuid.lower() in available:
                    await self._client.start_notify(uuid, handler)
                    return
            raise RuntimeError(self._no_cadence_message())
        # The GATT table could not be inspected: probe the characteristics
        # instead, so an old bleak still gets the fallback.
        last_error: Optional[BaseException] = None
        for uuid, handler in candidates:
            try:
                await self._client.start_notify(uuid, handler)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(self._no_cadence_message()) from last_error

    def _characteristic_uuids(self) -> Optional[Set[str]]:
        return _client_characteristic_uuids(self._client)

    def _no_cadence_message(self) -> str:
        address = getattr(self._client, "address", None) or "device"
        return (
            f"{address} exposes neither Cycling Speed and Cadence 0x2A5B nor "
            f"Cycling Power 0x2A63 crank data, so wattracker cannot read "
            f"cadence from it."
        )

    def _on_notify(self, _char, data: bytearray) -> None:
        parsed = parse_csc_measurement(bytes(data))
        self._cranks.update(parsed["crank_revs"], parsed["crank_event_time"])

    def _on_power_notify(self, _char, data: bytearray) -> None:
        """Crank revolutions carried by a Cycling Power measurement; the watts
        in the same packet are deliberately discarded."""
        parsed = parse_cycling_power_measurement(bytes(data))
        self._cranks.update(parsed["crank_revs"], parsed["crank_event_time"])

    def latest_power(self) -> Optional[int]:
        return None

    def latest_cadence(self) -> Optional[float]:
        return self._cranks.latest(self._stale_after_s)


class SharedCadenceSource(CadenceSource, PowerSource):
    """Cadence borrowed from another role already notifying on the same client.

    A Wahoo KICKR selected for both power and cadence gets one client, and
    macOS rejects a second ``start_notify`` on the characteristic the power
    role already subscribed to. This adapter reads that role's cadence instead
    of opening a duplicate subscription, and keeps ``BleakCadenceSource``'s
    cadence-only contract: ``latest_power()`` is always ``None``.
    """

    def __init__(self, source) -> None:
        self._source = source

    def detach(self) -> None:
        """Drop the borrowed source; called when its device disconnects."""
        self._source = None

    def latest_power(self) -> Optional[int]:
        return None

    def latest_cadence(self) -> Optional[float]:
        if self._source is None:
            return None
        return self._source.latest_cadence()


# Preference order BleakCadenceSource.start() subscribes in.
CADENCE_CHARACTERISTICS = (
    CYCLING_SPEED_AND_CADENCE_MEASUREMENT,
    CYCLING_POWER_MEASUREMENT,
)


def _planned_cadence_characteristic(client) -> Optional[str]:
    """The characteristic ``BleakCadenceSource.start()`` would subscribe to.

    ``None`` when the GATT table cannot be inspected (``start()`` then probes)
    or when the device exposes no cadence characteristic at all.
    """
    available = _client_characteristic_uuids(client)
    if available is None:
        return None
    for uuid in CADENCE_CHARACTERISTICS:
        if uuid.lower() in available:
            return uuid
    return None


def _shared_cadence_provider(bound_roles: dict, characteristic: Optional[str]):
    """An already-bound role on this client reading cadence from ``characteristic``.

    ``None`` when nothing can be shared and the cadence role must subscribe
    itself.
    """
    if not characteristic:
        return None
    wanted = characteristic.lower()
    for source in bound_roles.values():
        owned = getattr(source, "CADENCE_CHARACTERISTIC", None)
        if owned and owned.lower() == wanted and hasattr(source, "latest_cadence"):
            return source
    return None


class BleakHeartRateSource(HeartRateSource):
    def __init__(self, client, stale_after_s: float = BLE_VALUE_STALE_S) -> None:
        self._client = client
        self._stale_after_s = float(stale_after_s)
        self._hr: Optional[int] = None
        self._hr_updated_at: Optional[float] = None

    async def start(self) -> None:
        await self._client.start_notify(HEART_RATE_MEASUREMENT, self._on_notify)

    def _on_notify(self, _char, data: bytearray) -> None:
        self._hr = parse_heart_rate_measurement(bytes(data))["hr"]
        self._hr_updated_at = time.monotonic()

    def latest_hr(self) -> Optional[int]:
        if (
            self._hr_updated_at is None
            or time.monotonic() - self._hr_updated_at > self._stale_after_s
        ):
            return None
        return self._hr


class BleakTrainer(Trainer):
    """FTMS trainer over bleak: request control, start, then set ERG targets.

    Every control-point procedure is serialized and completed only after the
    matching indication arrives. This is required by FTMS and prevents Request
    Control, Start/Resume and target writes from racing one another.
    """

    def __init__(self, client, response_timeout_s: float = 2.0) -> None:
        self._client = client
        self._response_timeout_s = float(response_timeout_s)
        self._procedure_lock = asyncio.Lock()
        self._notify_lock = asyncio.Lock()
        self._notify_started = False
        self._pending_op: Optional[int] = None
        self._pending_response = None
        self._tasks: Set[asyncio.Task] = set()
        self._erg_available = False
        self._erg_enabled = False
        self.last_response: Optional[dict] = None
        self.last_error: Optional[str] = None

    @property
    def erg_available(self) -> bool:
        return self._erg_available

    @property
    def erg_enabled(self) -> bool:
        return self._erg_enabled

    def _on_control_point(self, _char, data: bytearray) -> None:
        try:
            resp = parse_control_point_response(bytes(data))
        except ValueError as e:
            log.debug("Ignoring non-response FTMS indication: %s", e)
            return
        self.last_response = resp
        pending = self._pending_response
        if (
            pending is not None
            and not pending.done()
            and resp["request_op"] == self._pending_op
        ):
            pending.set_result(resp)
        if resp["success"]:
            log.debug("FTMS op 0x%02x acknowledged", resp["request_op"])
        else:
            log.warning(
                "FTMS op 0x%02x rejected: %s", resp["request_op"], resp["message"]
            )

    async def _ensure_notify(self) -> None:
        if self._notify_started:
            return
        async with self._notify_lock:
            if self._notify_started:
                return
            await self._client.start_notify(
                FITNESS_MACHINE_CONTROL_POINT, self._on_control_point
            )
            self._notify_started = True

    async def _procedure(self, payload: bytes, what: str) -> dict:
        async with self._procedure_lock:
            return await self._procedure_locked(payload, what)

    async def _procedure_locked(self, payload: bytes, what: str) -> dict:
        """Run one FTMS control-point procedure and await its acknowledgement.

        The caller must already hold ``self._procedure_lock``. Holding it across
        a multi-step operation (e.g. enable ERG: request-control -> start ->
        set-target) keeps that operation atomic, so a concurrent command such as
        set-target-power cannot interleave between the handshake steps.
        """
        request_op = payload[0]
        await self._ensure_notify()
        loop = asyncio.get_running_loop()
        response = loop.create_future()
        self._pending_op = request_op
        self._pending_response = response
        try:
            await self._client.write_gatt_char(
                FITNESS_MACHINE_CONTROL_POINT, payload, response=True
            )
            result = await asyncio.wait_for(
                response, timeout=self._response_timeout_s
            )
        except asyncio.TimeoutError as exc:
            message = f"FTMS {what} timed out waiting for acknowledgement"
            self.last_error = message
            raise TimeoutError(message) from exc
        except Exception as exc:
            self.last_error = f"FTMS {what} failed: {exc}"
            raise
        finally:
            self._pending_op = None
            self._pending_response = None
        if not result["success"]:
            message = f"FTMS {what} rejected: {result['message']}"
            self.last_error = message
            raise RuntimeError(message)
        self.last_error = None
        return result

    async def _write(self, payload: bytes, what: str) -> bool:
        """Compatibility wrapper returning success while retaining strict ACKs."""
        try:
            await self._procedure(payload, what)
            return True
        except Exception as e:
            log.warning("FTMS %s write failed: %s", what, e)
            return False

    async def prepare(self) -> None:
        """Put the trainer in ERG: subscribe to responses, take control, start."""
        await self.async_enable_erg()

    async def async_enable_erg(self, target_watts: Optional[int] = None) -> None:
        # Hold the procedure lock across the whole handshake so no other command
        # (e.g. a concurrent set-target-power) interleaves between request
        # control, start, and the initial target.
        async with self._procedure_lock:
            self._erg_enabled = False
            await self._procedure_locked(
                encode_request_control(), "request control"
            )
            await self._procedure_locked(encode_start(), "start")
            self._erg_available = True
            try:
                if target_watts is not None:
                    await self._procedure_locked(
                        encode_set_target_power(target_watts), "set target power"
                    )
            except Exception:
                self._erg_enabled = False
                raise
            self._erg_enabled = True

    async def async_set_target_power(self, watts: int) -> None:
        try:
            await self._procedure(
                encode_set_target_power(watts), "set target power"
            )
        except Exception:
            self._erg_enabled = False
            raise

    async def async_stop(self) -> None:
        await self._procedure(encode_stop(), "stop")
        self._erg_enabled = False

    async def async_disable_erg(self) -> None:
        await self.async_stop()

    def _schedule(self, coro) -> None:
        # Synchronous entry point: schedule the async write if an event loop is
        # running; otherwise run it to completion.
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(coro)
            self._tasks.add(task)

            def _done(completed: asyncio.Task) -> None:
                self._tasks.discard(completed)
                try:
                    completed.result()
                except Exception as exc:
                    log.warning("FTMS command failed: %s", exc)

            task.add_done_callback(_done)
        except RuntimeError:
            asyncio.run(coro)

    def set_target_power(self, watts: int) -> None:
        self._schedule(self.async_set_target_power(watts))

    def start_erg(self) -> None:
        self._schedule(self.async_enable_erg())

    def stop_erg(self) -> None:
        self._schedule(self.async_stop())


async def scan(timeout: float = 5.0, attempts: int = SCAN_ATTEMPTS) -> List[dict]:
    """Discover nearby BLE devices grouped by advertised service.

    Requires ``bleak`` + an adapter. Raises RuntimeError when unavailable.
    Multiple sweeps reduce intermittent advertising misses; sightings are
    merged by the adapter's opaque address.
    """
    ok, reason = bleak_available()
    if not ok:
        raise RuntimeError(f"Bluetooth unavailable: {reason}")
    from bleak import BleakScanner  # type: ignore

    attempts = max(1, int(attempts))
    merged: Dict[str, dict] = {}
    failures = []
    for _attempt in range(attempts):
        try:
            discovered = await BleakScanner.discover(
                timeout=timeout, return_adv=True
            )
        except Exception as exc:
            failures.append(exc)
            continue
        for _addr, (device, adv) in discovered.items():
            address = device.address
            services = [
                service.lower() for service in (adv.service_uuids or [])
            ]
            name = device.name or adv.local_name or "(unknown)"
            rssi = adv.rssi
            sighting = merged.setdefault(
                address,
                {
                    "address": address,
                    "name": name,
                    "services": [],
                    "roles": [],
                    "rssi": rssi,
                },
            )
            if sighting["name"] == "(unknown)" and name != "(unknown)":
                sighting["name"] = name
            for service in services:
                if service not in sighting["services"]:
                    sighting["services"].append(service)
            if rssi is not None and (
                sighting["rssi"] is None or rssi > sighting["rssi"]
            ):
                sighting["rssi"] = rssi

    if not merged and len(failures) == attempts:
        details = "; ".join(str(exc) for exc in failures if str(exc))
        raise RuntimeError(
            "Bluetooth scan failed on every attempt"
            + (f": {details}" if details else ".")
        ) from failures[-1]

    for sighting in merged.values():
        services = sighting["services"]
        if CYCLING_POWER_SERVICE in services:
            sighting["roles"].append("power")
        if HEART_RATE_SERVICE in services:
            sighting["roles"].append("hr")
        if FITNESS_MACHINE_SERVICE in services:
            sighting["roles"].append("trainer")
        if CYCLING_SPEED_AND_CADENCE_SERVICE in services:
            sighting["roles"].append("cadence")
    return list(merged.values())


async def connect_sensors(
    timeout: float = 6.0,
    selected: Optional[dict] = None,
    retry_delay: Optional[float] = None,
) -> dict:
    """Connect selected sensors, or auto-discover the first sensor per role.

    Returns ``{"trainer", "power_source", "cadence_source", "hr_source", "clients", "names"}``.
    Any role may be None (graceful degradation: e.g. power-only
    with no controllable trainer). The caller must disconnect every client in
    ``clients`` when the ride ends. Raises RuntimeError when bleak/adapter is
    unavailable. ``selected`` maps ``power`` to zero or more opaque addresses
    and ``trainer`` / ``hr`` / ``cadence`` to zero or one. Explicit addresses are never
    replaced with a different discovered device.
    """
    ok, reason = bleak_available()
    if not ok:
        raise RuntimeError(f"Bluetooth unavailable: {reason}")
    from bleak import BleakClient  # type: ignore
    if retry_delay is None:
        retry_delay = CONNECT_RETRY_DELAY_S

    roles: dict = {"trainer": [], "power": [], "hr": [], "cadence": []}
    if selected is None:
        found = await scan(timeout=timeout)

        def _first_with(service_uuid: str) -> Optional[dict]:
            for device in found:
                if service_uuid in [s.lower() for s in device["services"]]:
                    return device
            return None

        for role, service in (
            ("trainer", FITNESS_MACHINE_SERVICE),
            ("power", CYCLING_POWER_SERVICE),
            ("hr", HEART_RATE_SERVICE),
            ("cadence", CYCLING_SPEED_AND_CADENCE_SERVICE),
        ):
            device = _first_with(service)
            if device:
                roles[role].append(device)
    else:
        for role in roles:
            raw_addresses = selected.get(role, [])
            if isinstance(raw_addresses, str):
                raw_addresses = [raw_addresses]
            seen = set()
            for address in raw_addresses:
                if address in seen:
                    continue
                seen.add(address)
                roles[role].append({"address": address, "name": address})

    clients: dict = {}  # address -> connected BleakClient (dedup: one per device)
    out = {"trainer": None, "power_source": None, "cadence_source": None,
           "hr_source": None,
           "clients": [], "clients_by_address": clients, "bindings": {},
           "names": {}, "errors": []}
    power_sources = []
    power_names = []
    cadence_sources = []
    cadence_names = []
    try:
        for role in ("trainer", "power", "hr", "cadence"):
            for dev in roles[role]:
                addr = dev["address"]
                client = clients.get(addr)
                if client is None:
                    final_error = None
                    timed_out = False
                    for attempt in range(2):
                        client = BleakClient(addr)
                        try:
                            await asyncio.wait_for(
                                client.connect(), timeout=CONNECT_TIMEOUT_S
                            )
                            final_error = None
                            break
                        except asyncio.TimeoutError as exc:
                            final_error = exc
                            timed_out = True
                        except BaseException as exc:
                            final_error = exc
                            timed_out = False
                        finally:
                            if final_error is not None:
                                try:
                                    await client.disconnect()
                                except BaseException:
                                    pass
                        if not isinstance(final_error, Exception):
                            raise final_error
                        if attempt == 0 and retry_delay > 0:
                            await asyncio.sleep(retry_delay)
                    if final_error is not None:
                        if timed_out:
                            message = (
                                f"Timed out connecting {role} sensor {dev['name']} "
                                f"({addr}) — it may still be held by another app or a "
                                f"previous ride; wait a few seconds and retry."
                            )
                        else:
                            message = (
                                f"Could not connect {role} sensor {dev['name']} "
                                f"({addr}): {final_error}"
                            )
                        log.warning(message)
                        out["errors"].append(message)
                        continue
                    clients[addr] = client
                    out["clients"].append(client)
                try:
                    if role == "trainer":
                        trainer = BleakTrainer(client)
                        await trainer.prepare()
                        out["trainer"] = trainer
                        out["names"][role] = dev["name"]
                        out["bindings"].setdefault(
                            addr, {"name": dev["name"], "roles": {}}
                        )["roles"][role] = trainer
                    elif role == "power":
                        source = BleakPowerSource(client)
                        await source.start()
                        power_sources.append(source)
                        power_names.append(dev["name"])
                        out["bindings"].setdefault(
                            addr, {"name": dev["name"], "roles": {}}
                        )["roles"][role] = source
                    elif role == "hr":
                        hr = BleakHeartRateSource(client)
                        await hr.start()
                        out["hr_source"] = hr
                        out["names"][role] = dev["name"]
                        out["bindings"].setdefault(
                            addr, {"name": dev["name"], "roles": {}}
                        )["roles"][role] = hr
                    elif role == "cadence":
                        bound = out["bindings"].get(addr, {}).get("roles", {})
                        shared = _shared_cadence_provider(
                            bound,
                            _planned_cadence_characteristic(client),
                        )
                        if shared is not None:
                            # Same device already notifying on that
                            # characteristic for another role: borrow it, since
                            # a second subscription is rejected by the OS.
                            cadence = SharedCadenceSource(shared)
                        else:
                            cadence = BleakCadenceSource(client)
                            await cadence.start()
                        cadence_sources.append(cadence)
                        cadence_names.append(dev["name"])
                        out["bindings"].setdefault(
                            addr, {"name": dev["name"], "roles": {}}
                        )["roles"][role] = cadence
                except Exception as e:  # role setup failed: keep the others working
                    message = f"Could not set up {role} sensor {dev['name']} ({addr}): {e}"
                    log.warning(message)
                    out["errors"].append(message)
    except BaseException:
        for client in reversed(out["clients"]):
            try:
                await client.disconnect()
            except BaseException:
                pass
        raise

    # A successful transport connection is useful only when at least one role
    # was initialized. Release setup orphans, while retaining shared-address
    # clients where another role on that same device succeeded.
    for addr, client in list(clients.items()):
        if out["bindings"].get(addr, {}).get("roles"):
            continue
        try:
            await client.disconnect()
        except BaseException:
            pass
        clients.pop(addr, None)
        try:
            out["clients"].remove(client)
        except ValueError:
            pass

    if power_sources:
        out["power_source"] = (
            power_sources[0]
            if len(power_sources) == 1
            else AggregatePowerSource(power_sources)
        )
        out["names"]["power"] = (
            power_names[0] if len(power_names) == 1 else power_names
        )
    if cadence_sources:
        out["cadence_source"] = cadence_sources[0]
        out["names"]["cadence"] = (
            cadence_names[0] if len(cadence_names) == 1 else cadence_names
        )
        # Power-source cadence retains its existing meaning when power exists.
        # A cadence-only connection remains usable by legacy consumers.
        if out["power_source"] is None:
            out["power_source"] = out["cadence_source"]
    return out


def _rebuild_connection_roles(conn: dict) -> None:
    """Rebuild public role objects/names after one device is removed."""
    powers = []
    power_names = []
    trainer = None
    trainer_name = None
    hr_source = None
    hr_name = None
    cadence_source = None
    cadence_name = None
    for binding in conn.get("bindings", {}).values():
        roles = binding.get("roles", {})
        name = binding.get("name", "(unknown)")
        if "power" in roles:
            powers.append(roles["power"])
            power_names.append(name)
        if trainer is None and "trainer" in roles:
            trainer = roles["trainer"]
            trainer_name = name
        if hr_source is None and "hr" in roles:
            hr_source = roles["hr"]
            hr_name = name
        if cadence_source is None and "cadence" in roles:
            cadence_source = roles["cadence"]
            cadence_name = name
    conn["power_source"] = (
        None
        if not powers
        else powers[0]
        if len(powers) == 1
        else AggregatePowerSource(powers)
    )
    conn["cadence_source"] = cadence_source
    if conn["power_source"] is None:
        conn["power_source"] = cadence_source
    conn["trainer"] = trainer
    conn["hr_source"] = hr_source
    names = {}
    if power_names:
        names["power"] = power_names[0] if len(power_names) == 1 else power_names
    if trainer_name is not None:
        names["trainer"] = trainer_name
    if hr_name is not None:
        names["hr"] = hr_name
    if cadence_name is not None:
        names["cadence"] = cadence_name
    conn["names"] = names


async def disconnect_sensor(conn: dict, address: str) -> dict:
    """Disconnect exactly one BLE address and remove every role it provided."""
    clients_by_address: Dict[str, object] = conn.get("clients_by_address", {})
    if address not in clients_by_address:
        raise ValueError("Device is not connected.")
    client = clients_by_address[address]
    trainer = (
        conn.get("bindings", {}).get(address, {}).get("roles", {}).get("trainer")
    )
    if trainer is not None:
        try:
            async_disable = getattr(trainer, "async_disable_erg", None)
            async_stop = getattr(trainer, "async_stop", None)
            if callable(async_disable):
                await async_disable()
            elif callable(async_stop):
                await async_stop()
            else:
                trainer.stop_erg()
        except Exception as exc:
            log.warning("Could not release trainer before disconnect: %s", exc)
    try:
        await client.disconnect()
    except Exception as exc:
        raise RuntimeError(f"Could not disconnect device: {exc}") from exc
    clients_by_address.pop(address, None)
    removed = conn.get("bindings", {}).pop(address, None) or {}
    cadence = removed.get("roles", {}).get("cadence")
    detach = getattr(cadence, "detach", None)
    if callable(detach):
        detach()  # never leave an adapter pointing at a disconnected source
    try:
        conn.get("clients", []).remove(client)
    except ValueError:
        pass
    _rebuild_connection_roles(conn)
    return conn
