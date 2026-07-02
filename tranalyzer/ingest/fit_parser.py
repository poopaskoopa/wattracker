"""Parse a Garmin/ANT+ .fit file into per-second streams + a summary."""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional

import fitdecode

# Record fields we care about, mapped to our stream keys.
_FIELD_MAP = {
    "power": "power",
    "cadence": "cadence",
    "heart_rate": "heartrate",
    "distance": "distance",
    "altitude": "altitude",
    "enhanced_altitude": "altitude",
}


def parse_fit(path: str) -> Dict:
    """Parse a .fit file.

    Returns a dict:
      {
        "start_time": ISO8601 str or None,
        "duration_s": int,
        "streams": {"time": [...], "power": [...], "cadence": [...],
                    "heartrate": [...], "distance": [...], "altitude": [...]},
      }
    """
    streams: Dict[str, List] = {
        "time": [],
        "power": [],
        "cadence": [],
        "heartrate": [],
        "distance": [],
        "altitude": [],
    }
    timestamps: List[_dt.datetime] = []

    with fitdecode.FitReader(path) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name != "record":
                continue

            ts = _get(frame, "timestamp")
            if isinstance(ts, _dt.datetime):
                timestamps.append(ts)
                streams["time"].append(ts.isoformat())
            else:
                streams["time"].append(None)

            for fit_field, key in _FIELD_MAP.items():
                if frame.has_field(fit_field):
                    val = _get(frame, fit_field)
                    # Only set once per record (first match wins for altitude).
                    if key == "altitude" and streams["altitude"] and \
                            len(streams["altitude"]) == len(streams["time"]):
                        continue
                    _append_aligned(streams, key, val)

            # Ensure every stream stays aligned to the number of records.
            _pad(streams)

    start_time: Optional[str] = None
    duration_s = 0
    if timestamps:
        # Normalize to naive so all downstream window comparisons stay
        # naive-vs-naive (FIT timestamps are tz-aware UTC).
        from ..timeutil import to_naive

        start_time = to_naive(timestamps[0]).isoformat()
        duration_s = int((timestamps[-1] - timestamps[0]).total_seconds())
    if duration_s <= 0:
        duration_s = max(len(streams["time"]) - 1, len(streams["time"]))

    return {
        "start_time": start_time,
        "duration_s": duration_s,
        "streams": streams,
    }


def _get(frame, name):
    try:
        return frame.get_value(name)
    except (KeyError, Exception):
        return None


def _append_aligned(streams: Dict[str, List], key: str, val) -> None:
    target = len(streams["time"])
    # Bring the stream up to one-before target, then append.
    while len(streams[key]) < target - 1:
        streams[key].append(None)
    if len(streams[key]) < target:
        streams[key].append(val)


def _pad(streams: Dict[str, List]) -> None:
    n = len(streams["time"])
    for key, seq in streams.items():
        if key == "time":
            continue
        while len(seq) < n:
            seq.append(None)
