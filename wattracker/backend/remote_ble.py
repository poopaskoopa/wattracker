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
        # Index of the newest sample the connector says it has recorded, or
        # None if it has not said. This is what makes resuming after a drop
        # exact: the server asks for everything after the last index it saw,
        # so the seconds ridden while the link was down are replayed once and
        # the ones that got through are not replayed at all.
        self.index: Optional[int] = None

    def update(self, power=None, cadence=None, hr=None, index=None) -> None:
        self._at = self._clock()
        self._power = None if power is None else int(power)
        self._cadence = None if cadence is None else float(cadence)
        self._hr = None if hr is None else int(hr)
        if index is not None:
            try:
                self.index = int(index)
            except (TypeError, ValueError):
                pass

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


class RemoteCadenceSource:
    """Cadence as seen through the connector, with no implied power.

    Mirrors ``devices.BleakCadenceSource``, including its ``latest_power``
    returning None: server.py aliases a cadence-only connection into
    ``power_source`` for legacy consumers, and that alias must not report the
    frame's watts as if this sensor had measured them.
    """

    def __init__(self, sink: RemoteSampleSink) -> None:
        self._sink = sink

    def latest_power(self) -> Optional[int]:
        return None

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

    Takes a *resolver* rather than a session, because a connector that drops
    and reconnects arrives as a brand-new ConnectorSession. Holding the object
    would mean every command after a reconnect went to a corpse - which is
    invisible until the one moment it matters.
    """

    def __init__(self, resolve_session, address: Optional[str] = None) -> None:
        self.resolve_session = resolve_session
        self.address = address
        self._available = True
        self._enabled = False
        self.last_error: Optional[str] = None

    @property
    def _session(self):
        session = self.resolve_session()
        if session is None:
            raise ConnectorUnavailable("connector disconnected")
        return session

    # Explicit, not inherited or defaulted - see the module docstring.
    @property
    def erg_available(self) -> bool:
        return self._available

    @property
    def erg_enabled(self) -> bool:
        return self._enabled

    async def _command(
        self, enabled: bool, watts: Optional[int] = None,
        force_rearm: bool = False,
    ) -> None:
        result = await self._session.call(
            "ble.set_erg",
            {"enabled": bool(enabled),
             "watts": None if watts is None else int(watts),
             "force_rearm": bool(force_rearm)},
        ) or {}
        self._available = bool(result.get("available", True))
        self._enabled = bool(result.get("enabled", enabled))
        self.last_error = result.get("error")

    async def async_enable_erg(self, target_watts: Optional[int] = None) -> None:
        # Arming, not adjusting: FTMS Request Control + Start + target.
        await self._command(True, target_watts, force_rearm=True)

    async def prepare(self) -> None:
        await self.async_enable_erg()

    async def async_set_target_power(self, watts: int) -> None:
        # The cheap path - one 0x05 write. ``_set_connection_erg`` already
        # decides when a bare target is enough and when a re-arm is genuinely
        # needed; collapsing both onto the same RPC (as this used to) erased
        # that distinction and cost three FTMS writes on every 1 Hz tick.
        await self._command(True, watts, force_rearm=False)

    async def async_stop(self) -> None:
        await self._command(False)

    async def async_disable_erg(self) -> None:
        await self._command(False)


class RemoteConnection(dict):
    """The ``conn`` mapping, plus the sink the connector's samples land in.

    A dict subclass because server.py treats a connection as a mapping
    throughout (``conn["power_source"]``, ``conn.get("errors", [])``), and the
    ride handler should not have to care which backend built it.

    The connector is reached through a resolver rather than a captured
    session, so a ride outlives a reconnect: see RemoteTrainer.
    """

    def __init__(self, resolve_session, sink: RemoteSampleSink, **fields) -> None:
        super().__init__(**fields)
        self.resolve_session = resolve_session
        self.sink = sink

    @property
    def live_session(self):
        """The attached connector, or None while it is away."""
        return self.resolve_session()

    @property
    def session(self):
        """The attached connector, refusing rather than returning a corpse."""
        session = self.resolve_session()
        if session is None:
            raise ConnectorUnavailable("connector disconnected")
        return session

    def attach(self) -> bool:
        """Point the returning connector's samples at this ride's sink.

        A reconnect brings a new ConnectorSession with ``ble_sink`` unset, so
        without this the samples arrive and are dropped on the floor.
        """
        session = self.resolve_session()
        if session is None:
            return False
        session.ble_sink = self.sink
        return True


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
    for role in ("hr", "trainer", "cadence"):
        for d in devices:
            if role in d.get("roles", []):
                names[role] = d["name"]
                break
    return names


def _sources_from_devices(devices: List[dict], sink: RemoteSampleSink, resolve):
    """Bind the connector's device rows to role sources.

    One implementation for both the initial connect and the rebind after a
    per-device disconnect, because the roles have to come out identical either
    way - a rider who drops their power meter mid-ride must land in the same
    state as one who never selected it.

    The cadence-only alias is ``devices.connect_sensors``' rule, reproduced
    exactly: with no power meter, the cadence sensor also stands in as
    ``power_source`` so legacy consumers keep working. It is aliased as the
    *same object*, which is what lets ``server._connection_has_power`` tell the
    stand-in from a real power measurement by identity.
    """
    trainer_device = next((d for d in devices if "trainer" in d["roles"]), None)
    trainer = (
        RemoteTrainer(resolve, trainer_device["address"])
        if trainer_device else None
    )
    power_source = (
        RemotePowerSource(sink)
        if any("power" in d["roles"] for d in devices) else None
    )
    cadence_source = (
        RemoteCadenceSource(sink)
        if any("cadence" in d["roles"] for d in devices) else None
    )
    hr_source = (
        RemoteHeartRateSource(sink)
        if any("hr" in d["roles"] for d in devices) else None
    )
    if power_source is None and cadence_source is not None:
        power_source = cadence_source
    return trainer, power_source, cadence_source, hr_source


async def connect_sensors(
    session,
    timeout: float = 6.0,
    selected: Optional[dict] = None,
    ride: Optional[dict] = None,
    resolve_session=None,
) -> RemoteConnection:
    """Connect the connector's sensors and start it recording.

    ``ride`` carries the identity of the session about to be ridden - start
    time, name, FTP, plan workout. It is sent now rather than asked for later
    because the case it exists for is the link going away: the connector has
    to be able to describe what it recorded without being able to ask.

    ``resolve_session`` is how the returned connection finds the connector
    later. The default keeps the one we connected through, which is right for
    a single call; a caller that has to survive a reconnect (the ride handler)
    passes a lookup by user instead.
    """
    resolve = resolve_session or (lambda: session)
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
    trainer, power_source, cadence_source, hr_source = _sources_from_devices(
        devices, sink, resolve
    )

    conn = RemoteConnection(
        resolve,
        sink,
        trainer=trainer,
        power_source=power_source,
        cadence_source=cadence_source,
        hr_source=hr_source,
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


async def resume_ride(conn: RemoteConnection) -> "tuple[List[tuple], bool]":
    """Take a ride back over after a reconnect.

    Three things have to happen and none of them is optional: the returning
    connector's samples have to be pointed at this ride's sink again (a new
    session starts with none), the connector has to be told somebody is
    driving again (or its own watchdog ends the ride), and the seconds ridden
    while we were away have to come back so they land in the activity instead
    of becoming a hole in it.

    Returns ``(rows, still_riding)``. Rows are ``(power, cadence, hr)``, in
    order, ready to be ticked into a RideController one second at a time.
    ``still_riding`` is False when the connector ended the ride on its own
    while we were away - it does that if the rider stops for long enough with
    nobody driving - in which case the samples are still worth having but
    there is nothing left to carry on with.
    """
    conn.attach()
    since = 0 if conn.sink.index is None else conn.sink.index + 1
    result = await conn.session.call("ble.catchup", {"since": since}) or {}
    rows: List[tuple] = []
    for row in result.get("samples") or []:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            continue
        rows.append((row[0], row[1], row[2]))
    count = result.get("count")
    if isinstance(count, int) and count > 0:
        # Where the live stream resumes from. Never wound backwards: the sink
        # is attached before the call, so frames that arrived during it are
        # already further ahead than the snapshot the connector answered with,
        # and rewinding would replay them again at the next drop.
        conn.sink.index = max(count - 1, conn.sink.index or 0)
    still_riding = bool(result.get("active"))
    log.info(
        "resumed a ride through the connector: replaying %d sample(s) from "
        "%d%s", len(rows), since,
        "" if still_riding else " (the connector has already ended it)",
    )
    return rows, still_riding


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

    trainer, power_source, cadence_source, hr_source = _sources_from_devices(
        devices, conn.sink, conn.resolve_session
    )
    conn["trainer"] = trainer
    conn["power_source"] = power_source
    conn["cadence_source"] = cadence_source
    conn["hr_source"] = hr_source
    conn["clients_by_address"] = {d["address"]: None for d in devices}
    conn["bindings"] = {
        d["address"]: {"name": d["name"], "roles": d["roles"]} for d in devices
    }
    conn["names"] = _names_from_devices(devices)
    conn["devices"] = devices
    return conn
