"""Bluetooth Low Energy ride support.

Everything here is import-safe without ``bleak`` and without a Bluetooth
adapter. The protocol parsers and the RideController state machine are pure and
fully unit-testable with simulated devices; the bleak-backed device classes are
the only hardware-dependent part and are guarded behind optional imports.
"""

from .protocol import (  # noqa: F401
    CYCLING_POWER_SERVICE,
    CYCLING_SPEED_AND_CADENCE_SERVICE,
    HEART_RATE_SERVICE,
    FITNESS_MACHINE_SERVICE,
    parse_cycling_power_measurement,
    parse_csc_measurement,
    parse_heart_rate_measurement,
    cadence_from_cranks,
    encode_set_target_power,
    encode_request_control,
    encode_start,
)
