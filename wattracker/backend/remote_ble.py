"""BLE hardware reached through a connector, shaped exactly like the real thing.

``ride_ws`` in server.py drives a ride against ``ble/devices.py``: it calls
``connect_sensors``, hands the returned sources to ``RideController``, and
commands ERG through the returned trainer. This module provides the same three
functions with the same signatures and the same return shapes, so that handler
needs to know only *which module* to call.

That works because the seam is already narrow. ``RideController.poll`` reads
sources through exactly three methods - ``latest_power``, ``latest_cadence``,
``latest_hr`` - and in the real-hardware path the websocket passes
``manage_trainer_commands=False``, so the controller never touches the trainer
at all. Upstream this is three scalars a second; downstream it is three
commands.

Two traps, both load-bearing in the local implementation and therefore
reproduced here rather than left to chance:

* ``BLE_VALUE_STALE_S``. The real sources return ``None`` once a reading is
  older than three seconds. Without that here, a dead network link would look
  to the controller like a rider holding a perfectly steady wattage - the
  clock would keep running on a ride nobody is pedalling.
* ``erg_available`` / ``erg_enabled``. ``server._connection_erg_state`` reads
  both with ``getattr(..., True)`` defaults, so a proxy that merely forgot to
  define them would silently claim ERG works. They are explicit properties.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..ble.devices import BLE_VALUE_STALE_S
from ..rpc import ConnectorUnavailable

log = logging.getLogger(__name__)

# A scan sweeps for several seconds on the far end, so it needs longer than the
# default RPC budget.
_SCAN_TIMEOUT_S = 60.0
_CONNECT_TIMEOUT_S = 60.0


class RemoteSampleSink:
    """Holds the newest telemetry frame pushed by the connector.

    One object serves as power source, cadence source and heart-rate source,
    because that is how the data arrives - a single frame per second carrying
    all three. Freshness is judged per-frame, so losing the link invalidates
    every reading at once, which is exactly right: they all came from the same
    now-silent connector.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._at: Optional[float] = None
        self._power: Optional[int] = None
        self._cadence: Optional[float] = None
        self._hr: Optional[int] = None

    def update(self, power=None, cadence=None, hr=None) -> None:
        self._at = self._clock()
        self._power = None if power is None else int(power)
        self._cadence = None if cadence is None else float(cadence)
        self._hr = None if hr is None else int(hr)

    @property
    def fresh(self) -> bool:
        return (
            self._at is not None
            and (self._clock() - self._at) <= BLE_VALUE_STALE_S
        )

    def latest_power(self) -> Optional[int]:
        return self._power if self.fresh else None

    def latest_cadence(self) -> Optional[float]:
        return self._cadence if self.fresh else None

    def latest_hr(self) -> Optional[int]:
        return self._hr if self.fresh else None


class RemotePowerSource:
    """Power (and cadence) as seen through the connector."""

    def __init__(self, sink: RemoteSampleSink) -> None:
        self._sink = sink

    def latest_power(self) -> Optional[int]:
        return self._sink.latest_power()

    def latest_cadence(self) -> Optional[float]:
        return self._sink.latest_cadence()


class RemoteHeartRateSource:
    def __init__(self, sink: RemoteSampleSink) -> None:
        self._sink = sink

    def latest_hr(self) -> Optional[int]:
        return self._sink.latest_hr()


class RemoteTrainer:
    """ERG control, issued as RPCs.

    Only the async methods are implemented, because those are the ones
    ``server._set_connection_erg`` and ``_stop_ble_trainer`` prefer; they fall
    back to sync ones only when the async are absent. Implementing both would
    mean two paths to keep in step for no gain.
    """

    def __init__(self, session, address: Optional[str] = None) -> None:
        self._session = session
        self.address = address
        self._available = True
        self._enabled = False
        self.last_error: Optional[str] = None

    # Explicit, not inherited or defaulted - see the module docstring.
    @property
    def erg_available(self) -> bool:
        return self._available

    @property
    def erg_enabled(self) -> bool:
        return self._enabled

    async def _command(self, enabled: bool, watts: Optional[int] = None) -> None:
        result = await self._session.call(
            "ble.set_erg",
            {"enabled": bool(enabled),
             "watts": None if watts is None else int(watts)},
        ) or {}
        self._available = bool(result.get("available", True))
        self._enabled = bool(result.get("enabled", enabled))
        self.last_error = result.get("error")

    async def async_enable_erg(self, target_watts: Optional[int] = None) -> None:
        await self._command(True, target_watts)

    async def prepare(self) -> None:
        await self.async_enable_erg()

    async def async_set_target_power(self, watts: int) -> None:
        await self._command(True, watts)

    async def async_stop(self) -> None:
        await self._command(False)

    async def async_disable_erg(self) -> None:
        await self._command(False)


class RemoteConnection(dict):
    """The ``conn`` mapping, plus the sink the connector's samples land in.

    A dict subclass because server.py treats a connection as a mapping
    throughout (``conn["power_source"]``, ``conn.get("errors", [])``), and the
    ride handler should not have to care which backend built it.
    """

    def __init__(self, session, sink: RemoteSampleSink, **fields) -> None:
        super().__init__(**fields)
        self.session = session
        self.sink = sink


async def bluetooth_available(session) -> "tuple[bool, str]":
    try:
        result = await session.call("ble.available") or {}
    except ConnectorUnavailable as exc:
        return False, str(exc)
    return bool(result.get("available")), str(result.get("reason") or "")


async def scan(session, timeout: float = 5.0, attempts: int = 2) -> List[dict]:
    """Same row shape as devices.scan: address, name, services, roles, rssi."""
    rows = await session.call(
        "ble.scan", {"timeout": timeout, "attempts": attempts},
        timeout=_SCAN_TIMEOUT_S,
    ) or []
    out: List[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("address"):
            continue
        out.append({
            "address": str(row["address"]),
            "name": str(row.get("name") or "(unknown)"),
            "services": [str(s) for s in (row.get("services") or [])],
            "roles": [str(r) for r in (row.get("roles") or [])],
            "rssi": row.get("rssi"),
        })
    return out


def _names_from_devices(devices: List[dict]) -> Dict[str, Any]:
    """Build the legacy ``names`` map from explicit {address, name, roles}.

    devices.connect_sensors builds this map differently depending on whether an
    explicit selection was given - names when auto-discovering, addresses when
    selecting - which the ride page then has to guess at. The RPC always
    carries both, so this end can be consistent: a single power source stays a
    bare string and several become a list, matching what ride.html already
    handles, but the value is always the human-readable name.
    """
    names: Dict[str, Any] = {}
    powers = [d["name"] for d in devices if "power" in d.get("roles", [])]
    if len(powers) == 1:
        names["power"] = powers[0]
    elif powers:
        names["power"] = powers
    for role in ("hr", "trainer"):
        for d in devices:
            if role in d.get("roles", []):
                names[role] = d["name"]
                break
    return names


async def connect_sensors(
    session,
    timeout: float = 6.0,
    selected: Optional[dict] = None,
    ride: Optional[dict] = None,
) -> RemoteConnection:
    """Connect the connector's sensors and start it recording.

    ``ride`` carries the identity of the session about to be ridden - start
    time, name, FTP, plan workout. It is sent now rather than asked for later
    because the case it exists for is the link going away: the connector has
    to be able to describe what it recorded without being able to ask.
    """
    result = await session.call(
        "ble.connect",
        {"timeout": timeout, "selected": selected, **(ride or {})},
        timeout=_CONNECT_TIMEOUT_S,
    ) or {}
    devices = [d for d in (result.get("devices") or []) if isinstance(d, dict)]
    for d in devices:
        d["address"] = str(d.get("address") or "")
        d["name"] = str(d.get("name") or d["address"] or "(unknown)")
        d["roles"] = [str(r) for r in (d.get("roles") or [])]

    sink = RemoteSampleSink()
    has_power = any("power" in d["roles"] for d in devices)
    has_hr = any("hr" in d["roles"] for d in devices)
    trainer_device = next(
        (d for d in devices if "trainer" in d["roles"]), None
    )

    conn = RemoteConnection(
        session,
        sink,
        trainer=(
            RemoteTrainer(session, trainer_device["address"])
            if trainer_device else None
        ),
        power_source=RemotePowerSource(sink) if has_power else None,
        hr_source=RemoteHeartRateSource(sink) if has_hr else None,
        # No BLE clients on this side; the connector owns the radio and closes
        # everything itself when the ride's socket goes away.
        clients=[],
        clients_by_address={d["address"]: None for d in devices},
        bindings={d["address"]: {"name": d["name"], "roles": d["roles"]}
                  for d in devices},
        names=_names_from_devices(devices),
        errors=[str(e) for e in (result.get("errors") or [])],
        devices=devices,
    )
    # So the connector's ble.sample events know where to land.
    session.ble_sink = sink
    return conn


async def disconnect_sensor(conn: RemoteConnection, address: str) -> RemoteConnection:
    """Drop one device; mirrors devices.disconnect_sensor's contract."""
    if address not in conn.get("clients_by_address", {}):
        raise ValueError("Device is not connected.")
    result = await conn.session.call(
        "ble.disconnect", {"address": address}
    ) or {}
    devices = [d for d in (result.get("devices") or []) if isinstance(d, dict)]
    for d in devices:
        d["address"] = str(d.get("address") or "")
        d["name"] = str(d.get("name") or d["address"])
        d["roles"] = [str(r) for r in (d.get("roles") or [])]

    has_power = any("power" in d["roles"] for d in devices)
    has_hr = any("hr" in d["roles"] for d in devices)
    trainer_device = next((d for d in devices if "trainer" in d["roles"]), None)

    conn["power_source"] = RemotePowerSource(conn.sink) if has_power else None
    conn["hr_source"] = RemoteHeartRateSource(conn.sink) if has_hr else None
    conn["trainer"] = (
        RemoteTrainer(conn.session, trainer_device["address"])
        if trainer_device else None
    )
    conn["clients_by_address"] = {d["address"]: None for d in devices}
    conn["bindings"] = {
        d["address"]: {"name": d["name"], "roles": d["roles"]} for d in devices
    }
    conn["names"] = _names_from_devices(devices)
    conn["devices"] = devices
    return conn
