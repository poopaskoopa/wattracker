"""The bounded fraction profile shared by desktop and cloud presentation."""
from __future__ import annotations

import math

from ..ble.runner import flatten_session
from .planner import Session
from .present import segment_rows

MAX_PROFILE_BLOCKS = 512
MAX_PROFILE_TEXT = 160


def fraction_profile(session: Session) -> list[dict]:
    """Return graph/step blocks without converting targets to rider watts.

    The desktop's ``segment_rows`` remains the source of labels and coaching
    text.  ``flatten_session`` supplies the exact timeline the desktop graph
    draws, including expanded interval repetitions.  The result contains only
    finite scalar JSON values and is bounded by block count and text length.
    """
    blocks, total_s = flatten_session(session)
    rows = segment_rows(session, 1.0)
    if not blocks or len(blocks) > MAX_PROFILE_BLOCKS:
        return []

    boundaries: list[tuple[int, int, dict]] = []
    cursor = 0
    for row in rows:
        duration = int(row.get("duration_s") or 0)
        if duration <= 0:
            continue
        boundaries.append((cursor, cursor + duration, row))
        cursor += duration
    if cursor != total_s:
        return []

    result: list[dict] = []
    source_index = 0
    for start, end, flattened_kind, value in blocks:
        while (
            source_index + 1 < len(boundaries)
            and start >= boundaries[source_index][1]
        ):
            source_index += 1
        if not boundaries or not (
            boundaries[source_index][0] <= start < boundaries[source_index][1]
        ):
            return []
        row = boundaries[source_index][2]
        if flattened_kind == "ramp":
            target_start, target_end = value
        elif flattened_kind == "free":
            target_start = target_end = None
        else:
            target_start = target_end = value

        values = (target_start, target_end)
        if any(
            value is not None
            and (not isinstance(value, (int, float)) or not math.isfinite(value))
            for value in values
        ):
            return []
        item = {
            "start": int(start),
            "end": int(end),
            "duration_s": int(end - start),
            "segment": int(source_index),
            "target_start": (
                round(float(target_start), 4) if target_start is not None else None
            ),
            "target_end": (
                round(float(target_end), 4) if target_end is not None else None
            ),
            "kind": str(row.get("kind") or flattened_kind),
        }
        label = row.get("label")
        if isinstance(label, str) and label:
            item["label"] = label[:MAX_PROFILE_TEXT]
        text = row.get("text")
        if isinstance(text, str) and text:
            item["text"] = text[:MAX_PROFILE_TEXT]
        if flattened_kind == "free":
            item["free"] = True
        result.append(item)
    return result
