"""Render a Session to Zwift .zwo workout XML and write it to the Zwift folder."""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom

from .planner import Segment, Session

_NAME_RE = re.compile(r"(<name>)(.*?)(</name>)", re.DOTALL)


def dated_name_zwo(zwo_str: str, date_iso: str) -> str:
    """Prefix the .zwo's internal <name> with the date (idempotent).

    Zwift lists custom workouts by their internal <name>, so a date prefix lets
    the user tell which day's workout is which. Skips prefixing when the name
    already starts with the date.
    """
    def repl(m: "re.Match") -> str:
        inner = m.group(2)
        if inner.strip().startswith(date_iso):
            return m.group(0)
        return f"{m.group(1)}{date_iso} {inner}{m.group(3)}"

    return _NAME_RE.sub(repl, zwo_str, count=1)


def _fmt_power(frac: float) -> str:
    """Format a power fraction of FTP (e.g. 0.9 -> '0.9')."""
    return f"{float(frac):.3f}".rstrip("0").rstrip(".")


def _add_text(parent: ET.Element, segment: Segment) -> None:
    """Attach a coaching textevent (at offset 0) if the segment has text."""
    if segment.text:
        ET.SubElement(
            parent,
            "textevent",
            {"timeoffset": "0", "message": segment.text},
        )


def session_to_zwo(session: Session, author: str = "wattracker") -> str:
    """Render a Session to a valid .zwo XML string."""
    root = ET.Element("workout_file")
    ET.SubElement(root, "author").text = author
    ET.SubElement(root, "name").text = session.name
    ET.SubElement(root, "description").text = (
        f"{session.description} (est. TSS {session.estimated_tss})"
    )
    ET.SubElement(root, "sportType").text = "cycling"
    workout = ET.SubElement(root, "workout")

    for seg in session.segments:
        if seg.kind == "warmup":
            el = ET.SubElement(
                workout,
                "Warmup",
                {
                    "Duration": str(int(seg.duration)),
                    "PowerLow": _fmt_power(seg.power_low or 0.0),
                    "PowerHigh": _fmt_power(seg.power_high or 0.0),
                },
            )
            _add_text(el, seg)
        elif seg.kind == "cooldown":
            el = ET.SubElement(
                workout,
                "Cooldown",
                {
                    "Duration": str(int(seg.duration)),
                    "PowerLow": _fmt_power(seg.power_low or 0.0),
                    "PowerHigh": _fmt_power(seg.power_high or 0.0),
                },
            )
            _add_text(el, seg)
        elif seg.kind == "steadystate":
            el = ET.SubElement(
                workout,
                "SteadyState",
                {
                    "Duration": str(int(seg.duration)),
                    "Power": _fmt_power(seg.power or 0.0),
                },
            )
            _add_text(el, seg)
        elif seg.kind == "intervals":
            el = ET.SubElement(
                workout,
                "IntervalsT",
                {
                    "Repeat": str(int(seg.repeat or 0)),
                    "OnDuration": str(int(seg.on_duration or 0)),
                    "OffDuration": str(int(seg.off_duration or 0)),
                    "OnPower": _fmt_power(seg.on_power or 0.0),
                    "OffPower": _fmt_power(seg.off_power or 0.0),
                },
            )
            _add_text(el, seg)
        elif seg.kind == "freeride":
            el = ET.SubElement(
                workout,
                "FreeRide",
                {"Duration": str(int(seg.duration))},
            )
            _add_text(el, seg)
        elif seg.kind == "ramp":
            el = ET.SubElement(
                workout,
                "Ramp",
                {
                    "Duration": str(int(seg.duration)),
                    "PowerLow": _fmt_power(seg.power_low or 0.0),
                    "PowerHigh": _fmt_power(seg.power_high or 0.0),
                },
            )
            _add_text(el, seg)

    rough = ET.tostring(root, encoding="unicode")
    return minidom.parseString(rough).toprettyxml(indent="    ")


def zwo_string(session: Session, author: str = "wattracker") -> str:
    """Return the .zwo XML string for download (alias of session_to_zwo)."""
    return session_to_zwo(session, author=author)


def _safe_filename(name: str) -> str:
    keep = [c if c.isalnum() or c in (" ", "-", "_") else "_" for c in name]
    base = "".join(keep).strip().replace(" ", "_")
    return (base or "workout") + ".zwo"


def plan_filename(date_iso: str, name: str) -> str:
    """Date-led, filesystem-safe .zwo filename, e.g. '2026-07-07 VO2 5x4.zwo'."""
    safe = "".join(c if (c.isalnum() or c in " -_.") else "_" for c in name).strip()
    safe = safe or "workout"
    return f"{date_iso} {safe}.zwo"


def write_plan_to_zwift(
    workouts: "list[dict]",
    zwift_id: str,
    workouts_override: "str | None" = None,
) -> dict:
    """Write each plan workout as a date-named .zwo into the Zwift folder.

    Each workout dict needs ``date``, ``name`` and ``zwo`` (the XML string).
    Returns {"directory": ..., "paths": [...], "count": N}.
    """
    from ..paths import workouts_dir

    target_dir = workouts_dir(zwift_id, override=workouts_override)
    os.makedirs(target_dir, exist_ok=True)
    written: "list[str]" = []
    for w in workouts:
        fname = plan_filename(w["date"], w["name"])
        p = os.path.join(target_dir, fname)
        # Date-prefix the internal <name> too, so Zwift's list shows the date.
        content = dated_name_zwo(w["zwo"], w["date"])
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(p)
    return {"directory": target_dir, "paths": written, "count": len(written)}


def write_to_zwift(
    zwo_str: str,
    zwift_id: str,
    name: str = "wattracker_Workout",
    workouts_override: "str | None" = None,
) -> str:
    """Write a .zwo string into the Zwift Workouts directory for `zwift_id`.

    A per-user `workouts_override` folder wins over the OS default. Creates the
    directory if it does not exist. Returns the written path.
    """
    from ..paths import workouts_dir

    target_dir = workouts_dir(zwift_id, override=workouts_override)
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, _safe_filename(name))
    with open(path, "w", encoding="utf-8") as f:
        f.write(zwo_str)
    return path
