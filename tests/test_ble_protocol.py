"""Tests for BLE GATT parsers/encoders against known byte payloads (no hardware)."""
import pytest

from tranalyzer.ble import protocol as p


# ------------------------------------------------ cycling power measurement
def test_cpm_power_only():
    # flags=0x0000, power=200W (0x00C8 LE)
    data = bytes([0x00, 0x00, 0xC8, 0x00])
    parsed = p.parse_cycling_power_measurement(data)
    assert parsed["power"] == 200
    assert parsed["crank_revs"] is None
    assert parsed["crank_event_time"] is None


def test_cpm_negative_power():
    # sint16: 0xFFFF = -1
    data = bytes([0x00, 0x00, 0xFF, 0xFF])
    assert p.parse_cycling_power_measurement(data)["power"] == -1


def test_cpm_with_crank_data():
    # flags bit5 (crank rev data present) = 0x0020, power=250 (0x00FA)
    # crank revs=10 (0x000A), last crank event time=1024 (0x0400)
    data = bytes([0x20, 0x00, 0xFA, 0x00, 0x0A, 0x00, 0x00, 0x04])
    parsed = p.parse_cycling_power_measurement(data)
    assert parsed["power"] == 250
    assert parsed["crank_revs"] == 10
    assert parsed["crank_event_time"] == 1024


def test_cpm_with_wheel_and_crank_offsets_correctly():
    # flags bit4|bit5 = 0x0030. After power: 6 wheel bytes, then 4 crank bytes.
    flags = bytes([0x30, 0x00])
    power = bytes([0x64, 0x00])  # 100W
    wheel = bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x08])  # 4B revs + 2B time
    crank = bytes([0x05, 0x00, 0x00, 0x04])  # revs=5, time=1024
    parsed = p.parse_cycling_power_measurement(flags + power + wheel + crank)
    assert parsed["power"] == 100
    assert parsed["crank_revs"] == 5
    assert parsed["crank_event_time"] == 1024


def test_cpm_too_short_raises():
    with pytest.raises(ValueError):
        p.parse_cycling_power_measurement(bytes([0x00, 0x00]))


# --------------------------------------------------------------- cadence
def test_cadence_60rpm():
    # 1 crank rev in 1024 ticks (= 1.0s) -> 60 rpm
    assert p.cadence_from_cranks(0, 0, 1, 1024) == pytest.approx(60.0)


def test_cadence_handles_uint16_wraparound():
    # time wraps: 65000 -> 488 is a delta of 1024 ticks (1s); 1 rev -> 60 rpm
    assert p.cadence_from_cranks(0, 65000, 1, 488) == pytest.approx(60.0)


def test_cadence_zero_when_no_delta():
    assert p.cadence_from_cranks(5, 1000, 5, 1000) == 0.0


def test_cadence_none_without_prev():
    assert p.cadence_from_cranks(None, None, 5, 1000) is None


# ----------------------------------------------- heart rate measurement
def test_hr_uint8():
    # flags=0x00 -> uint8 HR value
    assert p.parse_heart_rate_measurement(bytes([0x00, 150]))["hr"] == 150


def test_hr_uint16():
    # flags bit0=1 -> uint16 LE; 300 = 0x012C
    assert p.parse_heart_rate_measurement(bytes([0x01, 0x2C, 0x01]))["hr"] == 300


# --------------------------------------------------- FTMS control point
def test_encode_set_target_power():
    # op 0x05 + sint16 LE 250 (0x00FA)
    assert p.encode_set_target_power(250) == bytes([0x05, 0xFA, 0x00])


def test_encode_set_target_power_rounds_and_clamps():
    assert p.encode_set_target_power(199.6) == bytes([0x05, 0xC8, 0x00])  # 200


def test_encode_request_control_and_start():
    assert p.encode_request_control() == bytes([0x00])
    assert p.encode_start() == bytes([0x07])
