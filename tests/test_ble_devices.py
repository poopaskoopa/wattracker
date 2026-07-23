"""Pure/mocked tests for BLE discovery and exact sensor selection."""
import asyncio
import sys
import types

import pytest

from wattracker.ble import devices
from wattracker.ble.protocol import (
    CYCLING_POWER_SERVICE,
    FITNESS_MACHINE_SERVICE,
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


def test_bleak_heart_rate_expires_by_monotonic_age(monkeypatch):
    now = [50.0]
    monkeypatch.setattr(devices.time, "monotonic", lambda: now[0])
    source = devices.BleakHeartRateSource(object(), stale_after_s=3)
    source._on_notify(None, bytearray([0, 148]))
    assert source.latest_hr() == 148

    now[0] += 3.01
    assert source.latest_hr() is None


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
    assert "radio refused" in result["errors"][0]


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
            ],
            "roles": ["power", "hr", "trainer"],
            "rssi": -47,
        }
    ]
