"""BLE GATT protocol parsing/encoding for cycling sensors (pure, no hardware).

Covers:
  - Cycling Power Measurement (0x2A63) -> instantaneous power + crank data
  - Heart Rate Measurement (0x2A37)    -> heart rate bpm
  - Fitness Machine Control Point (0x2AD9) encoders -> ERG control

All functions operate on raw ``bytes`` and are fully unit-testable.
"""
from __future__ import annotations

from typing import Optional

# Service UUIDs (16-bit assigned numbers, and their 128-bit base forms).
CYCLING_POWER_SERVICE = "00001818-0000-1000-8000-00805f9b34fb"
HEART_RATE_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
FITNESS_MACHINE_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"

CYCLING_POWER_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
FITNESS_MACHINE_CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"

# Cycling Power Measurement flag bits (16-bit flags field).
_CPM_PEDAL_POWER_BALANCE = 1 << 0
_CPM_ACCUMULATED_TORQUE = 1 << 2
_CPM_WHEEL_REV_DATA = 1 << 4
_CPM_CRANK_REV_DATA = 1 << 5

# Crank event time resolution: units of 1/1024 second.
CRANK_TIME_RESOLUTION = 1024


def parse_cycling_power_measurement(data: bytes) -> dict:
    """Parse a Cycling Power Measurement (0x2A63) payload.

    Layout: uint16 flags, sint16 instantaneous power (W), then optional fields
    in a fixed order depending on the flags. Returns a dict with ``power`` and,
    when present, ``crank_revs`` / ``crank_event_time`` (else None).
    """
    if len(data) < 4:
        raise ValueError("CPM payload too short")
    flags = int.from_bytes(data[0:2], "little")
    power = int.from_bytes(data[2:4], "little", signed=True)

    idx = 4
    if flags & _CPM_PEDAL_POWER_BALANCE:
        idx += 1
    if flags & _CPM_ACCUMULATED_TORQUE:
        idx += 2
    if flags & _CPM_WHEEL_REV_DATA:
        idx += 6  # uint32 cumulative wheel revs + uint16 last wheel event time

    crank_revs: Optional[int] = None
    crank_event_time: Optional[int] = None
    if flags & _CPM_CRANK_REV_DATA and len(data) >= idx + 4:
        crank_revs = int.from_bytes(data[idx:idx + 2], "little")
        crank_event_time = int.from_bytes(data[idx + 2:idx + 4], "little")

    return {
        "flags": flags,
        "power": power,
        "crank_revs": crank_revs,
        "crank_event_time": crank_event_time,
    }


def cadence_from_cranks(
    prev_revs: Optional[int],
    prev_time: Optional[int],
    revs: Optional[int],
    time: Optional[int],
    resolution: int = CRANK_TIME_RESOLUTION,
) -> Optional[float]:
    """Compute cadence (rpm) from two consecutive crank samples.

    ``time`` is the last-crank-event-time in 1/1024s units (both uint16, so they
    wrap). Returns None when either sample lacks crank data; 0.0 when the crank
    hasn't turned (no time delta).
    """
    if None in (prev_revs, prev_time, revs, time):
        return None
    d_revs = (revs - prev_revs) & 0xFFFF
    d_time = (time - prev_time) & 0xFFFF
    if d_time == 0:
        return 0.0
    return d_revs * resolution * 60.0 / d_time


def parse_heart_rate_measurement(data: bytes) -> dict:
    """Parse a Heart Rate Measurement (0x2A37) payload.

    Flags bit0 selects uint8 (0) vs uint16 (1) HR value format.
    """
    if len(data) < 2:
        raise ValueError("HR payload too short")
    flags = data[0]
    if flags & 0x01:
        hr = int.from_bytes(data[1:3], "little")
    else:
        hr = data[1]
    return {"flags": flags, "hr": hr}


# ------------------------------------------------- FTMS control point (0x2AD9)
FTMS_REQUEST_CONTROL = 0x00
FTMS_SET_TARGET_POWER = 0x05
FTMS_START_RESUME = 0x07
FTMS_STOP_PAUSE = 0x08


def encode_request_control() -> bytes:
    """Op code 0x00: request control of the fitness machine."""
    return bytes([FTMS_REQUEST_CONTROL])


def encode_start() -> bytes:
    """Op code 0x07: start / resume."""
    return bytes([FTMS_START_RESUME])


def encode_set_target_power(watts: int) -> bytes:
    """Op code 0x05 + sint16 target power (W), little-endian. ERG control."""
    w = int(round(watts))
    w = max(-32768, min(32767, w))
    return bytes([FTMS_SET_TARGET_POWER]) + w.to_bytes(2, "little", signed=True)


def encode_stop() -> bytes:
    """Op code 0x08 + 0x01 (stop) parameter."""
    return bytes([FTMS_STOP_PAUSE, 0x01])
