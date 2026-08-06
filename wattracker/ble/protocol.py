"""BLE GATT protocol parsing/encoding for cycling sensors (pure, no hardware).

Covers:
  - Cycling Power Measurement (0x2A63) -> instantaneous power + crank data
  - CSC Measurement (0x2A5B)           -> crank data
  - Heart Rate Measurement (0x2A37)    -> heart rate bpm
  - Fitness Machine Control Point (0x2AD9) encoders -> ERG control

All functions operate on raw ``bytes`` and are fully unit-testable.
"""
from __future__ import annotations

from typing import Optional

# Service UUIDs (16-bit assigned numbers, and their 128-bit base forms).
CYCLING_POWER_SERVICE = "00001818-0000-1000-8000-00805f9b34fb"
CYCLING_SPEED_AND_CADENCE_SERVICE = "00001816-0000-1000-8000-00805f9b34fb"
HEART_RATE_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
FITNESS_MACHINE_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"

CYCLING_POWER_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
CYCLING_SPEED_AND_CADENCE_MEASUREMENT = "00002a5b-0000-1000-8000-00805f9b34fb"
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
    wrap). Returns None when either sample lacks crank data or the event time is
    unchanged, since repeated power notifications often carry the same last
    crank event and do not represent a new cadence observation. A new event
    time with no additional revolutions returns 0.0.
    """
    if None in (prev_revs, prev_time, revs, time):
        return None
    d_revs = (revs - prev_revs) & 0xFFFF
    d_time = (time - prev_time) & 0xFFFF
    if d_time == 0:
        return None
    return d_revs * resolution * 60.0 / d_time


def parse_csc_measurement(data: bytes) -> dict:
    """Parse a Cycling Speed and Cadence Measurement (0x2A5B) payload.

    The flags byte controls optional wheel (bit 0) and crank (bit 1) fields.
    Only crank data is needed for cadence. Returns ``crank_revs`` and
    ``crank_event_time`` as ``None`` when the sensor did not include them.
    """
    if not data:
        raise ValueError("CSC payload too short")
    flags = data[0]
    idx = 1
    if flags & 0x01:
        idx += 6  # uint32 cumulative wheel revs + uint16 last wheel event time

    crank_revs: Optional[int] = None
    crank_event_time: Optional[int] = None
    if flags & 0x02:
        if len(data) < idx + 4:
            raise ValueError("CSC payload missing crank data")
        crank_revs = int.from_bytes(data[idx:idx + 2], "little")
        crank_event_time = int.from_bytes(data[idx + 2:idx + 4], "little")
    return {
        "flags": flags,
        "crank_revs": crank_revs,
        "crank_event_time": crank_event_time,
    }


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
FTMS_RESPONSE_CODE = 0x80

# Control-point response result codes (FTMS spec 4.16.2.22).
FTMS_RESULT_SUCCESS = 0x01
FTMS_RESULT_NOT_SUPPORTED = 0x02
FTMS_RESULT_INVALID_PARAMETER = 0x03
FTMS_RESULT_OPERATION_FAILED = 0x04
FTMS_RESULT_CONTROL_NOT_PERMITTED = 0x05

_FTMS_RESULT_NAMES = {
    FTMS_RESULT_SUCCESS: "success",
    FTMS_RESULT_NOT_SUPPORTED: "op code not supported",
    FTMS_RESULT_INVALID_PARAMETER: "invalid parameter",
    FTMS_RESULT_OPERATION_FAILED: "operation failed",
    FTMS_RESULT_CONTROL_NOT_PERMITTED: "control not permitted",
}


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


def parse_control_point_response(data: bytes) -> dict:
    """Parse an FTMS control-point indication (response code 0x80).

    Layout: 0x80, request op code, result code. Returns ``request_op``,
    ``result``, ``success`` and a human-readable ``message``.
    """
    if len(data) < 3:
        raise ValueError("FTMS control point response too short")
    if data[0] != FTMS_RESPONSE_CODE:
        raise ValueError(f"not an FTMS response (first byte 0x{data[0]:02x})")
    request_op = data[1]
    result = data[2]
    return {
        "request_op": request_op,
        "result": result,
        "success": result == FTMS_RESULT_SUCCESS,
        "message": _FTMS_RESULT_NAMES.get(result, f"unknown result 0x{result:02x}"),
    }
