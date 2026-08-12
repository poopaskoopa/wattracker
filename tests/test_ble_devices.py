"""Pure/mocked tests for BLE discovery and exact sensor selection."""
import asyncio
import sys
import types

import pytest

from wattracker.ble import devices
from wattracker.ble.protocol import (
    CYCLING_POWER_MEASUREMENT,
    CYCLING_POWER_SERVICE,
    CYCLING_SPEED_AND_CADENCE_MEASUREMENT,
    CYCLING_SPEED_AND_CADENCE_SERVICE,
    FITNESS_MACHINE_SERVICE,
    HEART_RATE_MEASUREMENT,
    HEART_RATE_SERVICE,
)


class FixedPower(devices.PowerSource):
    def __init__(self, watts, cadence=None):
        self.watts = watts
        self.cadence = cadence

    def latest_power(self):
        return self.watts

    def latest_cadence(self):
        return self.cadence


def test_aggregate_power_source_sums_available_watts_and_averages_cadence():
    source = devices.AggregatePowerSource(
        [FixedPower(112, 90), FixedPower(108, 92), FixedPower(None, None)]
    )

    assert source.latest_power() == 220
    assert source.latest_cadence() == 91
    assert devices.AggregatePowerSource([FixedPower(None)]).latest_power() is None


def test_bleak_power_and_cadence_expire_by_monotonic_age(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    source = devices.BleakPowerSource(object(), stale_after_s=3)

    def measurement(power, revs, event_time):
        return bytearray(
            b"\x20\x00"
            + int(power).to_bytes(2, "little", signed=True)
            + int(revs).to_bytes(2, "little")
            + int(event_time).to_bytes(2, "little")
        )

    source._on_notify(None, measurement(105, 10, 1000))
    source._on_notify(None, measurement(110, 11, 2024))
    assert source.latest_power() == 110
    assert source.latest_cadence() == 60

    now[0] += 3.01
    assert source.latest_power() is None
    assert source.latest_cadence() is None


def test_bleak_power_duplicates_hold_cadence_without_refreshing_it(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    source = devices.BleakPowerSource(object(), stale_after_s=3)

    def measurement(power, revs, event_time):
        return bytearray(
            b"\x20\x00"
            + int(power).to_bytes(2, "little", signed=True)
            + int(revs).to_bytes(2, "little")
            + int(event_time).to_bytes(2, "little")
        )

    source._on_notify(None, measurement(150, 10, 1000))
    now[0] = 100.5
    source._on_notify(None, measurement(155, 11, 1878))
    assert source.latest_cadence() == pytest.approx(70.0, abs=0.1)

    now[0] = 101.0
    source._on_notify(None, measurement(160, 11, 1878))
    assert source.latest_power() == 160
    assert source.latest_cadence() == pytest.approx(70.0, abs=0.1)

    now[0] = 101.5
    source._on_notify(None, measurement(165, 12, 2756))
    assert source.latest_cadence() == pytest.approx(70.0, abs=0.1)

    now[0] = 103.0
    source._on_notify(None, measurement(170, 12, 2756))
    now[0] = 104.51
    assert source.latest_power() == 170
    assert source.latest_cadence() is None


def test_bleak_heart_rate_expires_by_monotonic_age(monkeypatch):
    now = [50.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    source = devices.BleakHeartRateSource(object(), stale_after_s=3)
    source._on_notify(None, bytearray([0, 148]))
    assert source.latest_hr() == 148

    now[0] += 3.01
    assert source.latest_hr() is None


def test_bleak_cadence_handles_wraparound_and_stale_duplicates(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    source = devices.BleakCadenceSource(object(), stale_after_s=3)

    def measurement(revs, event_time):
        return (
            bytearray([0x02])
            + revs.to_bytes(2, "little")
            + event_time.to_bytes(2, "little")
        )

    source._on_notify(None, measurement(65535, 65000))
    source._on_notify(None, measurement(0, 488))
    assert source.latest_cadence() == pytest.approx(60.0)
    now[0] = 101.0
    source._on_notify(None, measurement(0, 488))
    now[0] = 103.01
    assert source.latest_cadence() is None


class _FakeChar:
    def __init__(self, uuid):
        self.uuid = uuid


class _FakeService:
    def __init__(self, characteristics):
        self.characteristics = [_FakeChar(uuid) for uuid in characteristics]


class _FakeGattClient:
    """A connected client whose resolved GATT table can be inspected."""

    def __init__(self, characteristics, address="AA:BB:CC:DD:EE:FF"):
        self.address = address
        self.services = [_FakeService(characteristics)]
        self.notifies = {}

    async def start_notify(self, uuid, handler):
        self.notifies[uuid] = handler


def _csc_measurement(revs, event_time):
    return bytearray(
        bytes([0x02])
        + int(revs).to_bytes(2, "little")
        + int(event_time).to_bytes(2, "little")
    )


def _power_measurement(power, revs, event_time):
    return bytearray(
        b"\x20\x00"
        + int(power).to_bytes(2, "little", signed=True)
        + int(revs).to_bytes(2, "little")
        + int(event_time).to_bytes(2, "little")
    )


def test_cadence_source_uses_csc_when_the_device_exposes_it(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    client = _FakeGattClient(
        [CYCLING_SPEED_AND_CADENCE_MEASUREMENT, CYCLING_POWER_MEASUREMENT]
    )
    source = devices.BleakCadenceSource(client, stale_after_s=3)
    asyncio.run(source.start())

    assert list(client.notifies) == [CYCLING_SPEED_AND_CADENCE_MEASUREMENT]
    notify = client.notifies[CYCLING_SPEED_AND_CADENCE_MEASUREMENT]
    notify(None, _csc_measurement(10, 1000))
    notify(None, _csc_measurement(11, 2024))
    assert source.latest_cadence() == pytest.approx(60.0)
    assert source.latest_power() is None


def test_cadence_source_falls_back_to_cycling_power_cranks(monkeypatch):
    """A KICKR has no CSC service; its cranks ride in the power measurement."""
    now = [10.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    client = _FakeGattClient([CYCLING_POWER_MEASUREMENT])
    source = devices.BleakCadenceSource(client, stale_after_s=3)
    asyncio.run(source.start())

    assert list(client.notifies) == [CYCLING_POWER_MEASUREMENT]
    notify = client.notifies[CYCLING_POWER_MEASUREMENT]
    notify(None, _power_measurement(220, 10, 1000))
    now[0] = 10.5
    notify(None, _power_measurement(225, 11, 1878))
    assert source.latest_cadence() == pytest.approx(70.0, abs=0.1)
    # Cadence-only by contract, even while reading the power characteristic.
    assert source.latest_power() is None

    # A repeated crank event holds the value without refreshing its freshness.
    now[0] = 11.0
    notify(None, _power_measurement(230, 11, 1878))
    assert source.latest_cadence() == pytest.approx(70.0, abs=0.1)
    now[0] = 13.51
    assert source.latest_cadence() is None
    assert source.latest_power() is None


def test_cadence_source_names_the_device_when_no_crank_data_is_available():
    client = _FakeGattClient([HEART_RATE_MEASUREMENT], address="HR:01")
    source = devices.BleakCadenceSource(client)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(source.start())

    message = str(excinfo.value)
    assert "HR:01" in message
    assert "0x2A5B" in message and "0x2A63" in message
    assert "cannot read cadence" in message
    assert client.notifies == {}


def test_cadence_source_probes_when_the_gatt_table_is_not_inspectable():
    """Without a services collection, fall back by probing each notify."""

    class ProbeClient:
        address = "PROBE:01"

        def __init__(self, supported):
            self.supported = supported
            self.attempted = []
            self.notifies = {}

        async def start_notify(self, uuid, handler):
            self.attempted.append(uuid)
            if uuid not in self.supported:
                raise Exception(f"Characteristic {uuid} was not found!")
            self.notifies[uuid] = handler

    client = ProbeClient({CYCLING_POWER_MEASUREMENT})
    source = devices.BleakCadenceSource(client)
    asyncio.run(source.start())
    assert client.attempted == [
        CYCLING_SPEED_AND_CADENCE_MEASUREMENT, CYCLING_POWER_MEASUREMENT
    ]
    assert list(client.notifies) == [CYCLING_POWER_MEASUREMENT]

    barren = ProbeClient(set())
    with pytest.raises(RuntimeError, match="cannot read cadence"):
        asyncio.run(devices.BleakCadenceSource(barren).start())


def _install_fake_bleak(monkeypatch):
    module = types.ModuleType("bleak")

    class FakeClient:
        instances = []

        def __init__(self, address):
            self.address = address
            self.connected = False
            self.disconnected = False
            self.__class__.instances.append(self)

        async def connect(self):
            self.connected = True

        async def disconnect(self):
            self.connected = False
            self.disconnected = True

    module.BleakClient = FakeClient
    monkeypatch.setitem(sys.modules, "bleak", module)
    return module, FakeClient


def test_connect_sensors_uses_exact_selection_and_deduplicates_client(monkeypatch):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    async def no_scan(*_args, **_kwargs):
        raise AssertionError("explicit selection must not scan")

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__({"LEFT": 105, "RIGHT": 107}[client.address], 90)

        async def start(self):
            pass

    class FakeTrainer:
        def __init__(self, client):
            self.client = client
            self.stopped = False

        async def prepare(self):
            pass

        async def async_stop(self):
            self.stopped = True

    monkeypatch.setattr(devices, "scan", no_scan)
    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    monkeypatch.setattr(devices, "BleakTrainer", FakeTrainer)

    result = asyncio.run(
        devices.connect_sensors(
            selected={"power": ["LEFT", "RIGHT", "LEFT"], "trainer": ["LEFT"]}
        )
    )

    assert [client.address for client in fake_client.instances] == ["LEFT", "RIGHT"]
    assert result["trainer"].client is fake_client.instances[0]
    assert result["power_source"].latest_power() == 212
    assert result["names"]["power"] == ["LEFT", "RIGHT"]
    assert set(result["clients_by_address"]) == {"LEFT", "RIGHT"}
    assert set(result["bindings"]["LEFT"]["roles"]) == {"trainer", "power"}
    assert result["errors"] == []

    connected_trainer = result["trainer"]
    asyncio.run(devices.disconnect_sensor(result, "LEFT"))
    assert connected_trainer.stopped is True
    assert fake_client.instances[0].disconnected is True
    assert result["trainer"] is None
    assert result["power_source"].latest_power() == 107
    assert result["names"] == {"power": "RIGHT"}
    assert [client.address for client in result["clients"]] == ["RIGHT"]


def test_disconnect_sensor_rebuilds_dual_power_and_preserves_other_roles(
    monkeypatch,
):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__({"COMBO": 100, "RIGHT": 120}[client.address])

        async def start(self):
            pass

    class FakeHeart:
        def __init__(self, client):
            self.client = client

        async def start(self):
            pass

        def latest_hr(self):
            return 145

    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    monkeypatch.setattr(devices, "BleakHeartRateSource", FakeHeart)
    result = asyncio.run(
        devices.connect_sensors(
            selected={"power": ["COMBO", "RIGHT"], "hr": ["COMBO"]}
        )
    )
    assert result["power_source"].latest_power() == 220
    assert result["hr_source"].latest_hr() == 145

    asyncio.run(devices.disconnect_sensor(result, "RIGHT"))
    assert result["power_source"].latest_power() == 100
    assert result["hr_source"].latest_hr() == 145
    assert result["names"] == {"power": "COMBO", "hr": "COMBO"}
    assert fake_client.instances[1].disconnected is True

    asyncio.run(devices.disconnect_sensor(result, "COMBO"))
    assert result["power_source"] is None
    assert result["hr_source"] is None
    assert result["names"] == {}
    with pytest.raises(ValueError, match="not connected"):
        asyncio.run(devices.disconnect_sensor(result, "MISSING"))


def test_connect_sensors_reports_selected_setup_failure_and_keeps_other_power(
    monkeypatch,
):
    _install_fake_bleak(monkeypatch)

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__(123)
            self.address = client.address

        async def start(self):
            if self.address == "BAD":
                raise RuntimeError("notifications unavailable")

    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    result = asyncio.run(
        devices.connect_sensors(selected={"power": ["BAD", "GOOD"]})
    )

    assert result["power_source"].latest_power() == 123
    assert "BAD" in result["errors"][0]
    assert "notifications unavailable" in result["errors"][0]


def test_connect_sensors_disconnects_client_after_connect_failure(monkeypatch):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    async def fail_connect(self):
        raise RuntimeError("radio refused")

    monkeypatch.setattr(fake_client, "connect", fail_connect)
    result = asyncio.run(devices.connect_sensors(selected={"power": ["BAD"]}))

    assert result["clients"] == []
    assert fake_client.instances[0].disconnected is True
    assert fake_client.instances[1].disconnected is True
    assert "radio refused" in result["errors"][0]


def test_connect_sensors_connect_timeout_becomes_error_not_exception(monkeypatch):
    # A device the OS still holds (common with a KICKR right after a session)
    # can make client.connect() hang. The bounded connect must surface this as a
    # visible errors entry and continue, never hang or raise.
    _module, fake_client = _install_fake_bleak(monkeypatch)
    monkeypatch.setattr(devices, "CONNECT_TIMEOUT_S", 0.01)

    async def never_returns(self):
        await asyncio.Future()  # never completes

    monkeypatch.setattr(fake_client, "connect", never_returns)
    result = asyncio.run(devices.connect_sensors(selected={"power": ["STUCK"]}))

    assert result["clients"] == []
    assert result["power_source"] is None
    assert fake_client.instances[0].disconnected is True
    assert fake_client.instances[1].disconnected is True
    assert len(result["errors"]) == 1
    assert "Timed out connecting" in result["errors"][0]
    assert "STUCK" in result["errors"][0]


def test_connect_sensors_disconnects_client_when_connect_is_cancelled(monkeypatch):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    class CancelConnect(BaseException):
        pass

    async def cancel_connect(self):
        raise CancelConnect("cancelled")

    monkeypatch.setattr(fake_client, "connect", cancel_connect)
    with pytest.raises(CancelConnect):
        asyncio.run(devices.connect_sensors(selected={"power": ["CANCEL"]}))

    assert fake_client.instances[0].disconnected is True


def test_connect_sensors_retries_with_fresh_client_and_cleans_failure(monkeypatch):
    _module, fake_client = _install_fake_bleak(monkeypatch)
    monkeypatch.setattr(devices, "CONNECT_RETRY_DELAY_S", 0)

    async def flaky_connect(self):
        if len(fake_client.instances) == 1:
            raise RuntimeError("temporary radio failure")
        self.connected = True

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__(175)

        async def start(self):
            pass

    monkeypatch.setattr(fake_client, "connect", flaky_connect)
    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    result = asyncio.run(
        devices.connect_sensors(
            selected={"power": ["PEDALS"]}, retry_delay=0
        )
    )

    assert len(fake_client.instances) == 2
    assert fake_client.instances[0].disconnected is True
    assert result["clients"] == [fake_client.instances[1]]
    assert result["power_source"].latest_power() == 175
    assert result["errors"] == []


def test_connect_sensors_disconnects_client_when_all_role_setup_fails(
    monkeypatch,
):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    class BrokenPower(FixedPower):
        def __init__(self, client):
            super().__init__(100)

        async def start(self):
            raise RuntimeError("notify failed")

    monkeypatch.setattr(devices, "BleakPowerSource", BrokenPower)
    result = asyncio.run(
        devices.connect_sensors(selected={"power": ["ORPHAN"]})
    )

    assert result["clients"] == []
    assert result["clients_by_address"] == {}
    assert fake_client.instances[0].disconnected is True
    assert result["power_source"] is None
    assert "notify failed" in result["errors"][0]


def test_connect_sensors_preserves_shared_client_when_one_role_succeeds(
    monkeypatch,
):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    class BrokenTrainer:
        def __init__(self, client):
            pass

        async def prepare(self):
            raise RuntimeError("trainer setup failed")

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__(210)

        async def start(self):
            pass

    monkeypatch.setattr(devices, "BleakTrainer", BrokenTrainer)
    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    result = asyncio.run(
        devices.connect_sensors(
            selected={"trainer": ["COMBO"], "power": ["COMBO"]}
        )
    )

    assert len(fake_client.instances) == 1
    assert fake_client.instances[0].disconnected is False
    assert result["clients"] == [fake_client.instances[0]]
    assert result["power_source"].latest_power() == 210
    assert set(result["bindings"]["COMBO"]["roles"]) == {"power"}


def test_connect_sensors_disconnects_accumulated_clients_on_base_exception(
    monkeypatch,
):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    class FatalSetup(BaseException):
        pass

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__(100)
            self.address = client.address

        async def start(self):
            if self.address == "SECOND":
                raise FatalSetup("cancelled")

    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    with pytest.raises(FatalSetup):
        asyncio.run(
            devices.connect_sensors(selected={"power": ["FIRST", "SECOND"]})
        )

    assert [client.disconnected for client in fake_client.instances] == [True, True]


def test_connect_sensors_without_selection_preserves_first_device_auto_behavior(
    monkeypatch,
):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    async def fake_scan(timeout=5.0):
        return [
            {"address": "FIRST", "name": "First", "services": [CYCLING_POWER_SERVICE]},
            {"address": "SECOND", "name": "Second", "services": [CYCLING_POWER_SERVICE]},
        ]

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__(100)

        async def start(self):
            pass

    monkeypatch.setattr(devices, "scan", fake_scan)
    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    result = asyncio.run(devices.connect_sensors())

    assert [client.address for client in fake_client.instances] == ["FIRST"]
    assert result["names"]["power"] == "First"


def test_connects_cadence_only_and_rebuilds_shared_address_bindings(monkeypatch):
    _module, fake_client = _install_fake_bleak(monkeypatch)

    class FakePower(FixedPower):
        def __init__(self, client):
            super().__init__(200, 88)

        async def start(self):
            pass

    class FakeCadence:
        def __init__(self, client):
            self.client = client

        async def start(self):
            pass

        def latest_power(self):
            return None

        def latest_cadence(self):
            return 95

    monkeypatch.setattr(devices, "BleakPowerSource", FakePower)
    monkeypatch.setattr(devices, "BleakCadenceSource", FakeCadence)
    result = asyncio.run(
        devices.connect_sensors(selected={"power": ["COMBO"], "cadence": ["COMBO"]})
    )

    assert len(fake_client.instances) == 1
    assert result["power_source"].latest_cadence() == 88
    assert result["cadence_source"].latest_cadence() == 95
    assert result["names"] == {"power": "COMBO", "cadence": "COMBO"}
    assert set(result["bindings"]["COMBO"]["roles"]) == {"power", "cadence"}

    asyncio.run(devices.disconnect_sensor(result, "COMBO"))
    assert result["power_source"] is None
    assert result["cadence_source"] is None
    assert result["names"] == {}


def test_auto_selects_cadence_only_as_legacy_power_source(monkeypatch):
    _module, _fake_client = _install_fake_bleak(monkeypatch)

    async def fake_scan(timeout=5.0):
        return [
            {
                "address": "CAD",
                "name": "Cadence sensor",
                "services": [CYCLING_SPEED_AND_CADENCE_SERVICE],
            }
        ]

    class FakeCadence:
        def __init__(self, client):
            pass

        async def start(self):
            pass

        def latest_power(self):
            return None

        def latest_cadence(self):
            return 92

    monkeypatch.setattr(devices, "BleakCadenceSource", FakeCadence)
    monkeypatch.setattr(devices, "scan", fake_scan)
    result = asyncio.run(devices.connect_sensors())

    assert result["cadence_source"].latest_cadence() == 92
    assert result["power_source"] is result["cadence_source"]
    assert result["names"] == {"cadence": "Cadence sensor"}


def test_scan_returns_detected_roles_and_signal(monkeypatch):
    module, _fake_client = _install_fake_bleak(monkeypatch)

    class FakeScanner:
        @staticmethod
        async def discover(timeout, return_adv):
            assert return_adv is True
            device = types.SimpleNamespace(address="UUID-1", name=None)
            adv = types.SimpleNamespace(
                local_name="Combo",
                service_uuids=[
                    CYCLING_POWER_SERVICE.upper(),
                    HEART_RATE_SERVICE,
                    FITNESS_MACHINE_SERVICE,
                    CYCLING_SPEED_AND_CADENCE_SERVICE,
                ],
                rssi=-47,
            )
            return {device.address: (device, adv)}

    module.BleakScanner = FakeScanner
    found = asyncio.run(devices.scan(timeout=0.01))

    assert found == [
        {
            "address": "UUID-1",
            "name": "Combo",
            "services": [
                CYCLING_POWER_SERVICE,
                HEART_RATE_SERVICE,
                FITNESS_MACHINE_SERVICE,
                CYCLING_SPEED_AND_CADENCE_SERVICE,
            ],
            "roles": ["power", "hr", "trainer", "cadence"],
            "rssi": -47,
        }
    ]


def test_scan_merges_multiple_sweeps_and_tolerates_one_failure(monkeypatch):
    module, _fake_client = _install_fake_bleak(monkeypatch)
    calls = 0

    class FakeScanner:
        @staticmethod
        async def discover(timeout, return_adv):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("adapter warming up")
            weak = types.SimpleNamespace(address="COMBO", name=None)
            weak_adv = types.SimpleNamespace(
                local_name=None,
                service_uuids=[CYCLING_POWER_SERVICE],
                rssi=-70,
            )
            return {"COMBO": (weak, weak_adv)}

    module.BleakScanner = FakeScanner
    found = asyncio.run(devices.scan(timeout=0.01))

    assert calls == 2
    assert found[0]["address"] == "COMBO"
    assert found[0]["roles"] == ["power"]


def test_scan_merges_services_name_and_strongest_rssi_by_address(monkeypatch):
    module, _fake_client = _install_fake_bleak(monkeypatch)
    calls = 0

    class FakeScanner:
        @staticmethod
        async def discover(timeout, return_adv):
            nonlocal calls
            calls += 1
            device = types.SimpleNamespace(
                address="COMBO", name=None if calls == 1 else "Bike"
            )
            adv = types.SimpleNamespace(
                local_name=None,
                service_uuids=[
                    CYCLING_POWER_SERVICE
                    if calls == 1
                    else FITNESS_MACHINE_SERVICE
                ],
                rssi=-72 if calls == 1 else -44,
            )
            return {"COMBO": (device, adv)}

    module.BleakScanner = FakeScanner
    found = asyncio.run(devices.scan(timeout=0.01))

    assert found == [{
        "address": "COMBO",
        "name": "Bike",
        "services": [CYCLING_POWER_SERVICE, FITNESS_MACHINE_SERVICE],
        "roles": ["power", "trainer"],
        "rssi": -44,
    }]


def test_scan_raises_when_every_sweep_fails(monkeypatch):
    module, _fake_client = _install_fake_bleak(monkeypatch)

    class FakeScanner:
        @staticmethod
        async def discover(timeout, return_adv):
            raise RuntimeError("Bluetooth powered off")

    module.BleakScanner = FakeScanner
    with pytest.raises(RuntimeError, match="every attempt.*powered off"):
        asyncio.run(devices.scan(timeout=0.01))
