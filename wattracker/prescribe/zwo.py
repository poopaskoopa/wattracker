"""Render a Session to Zwift .zwo workout XML and write it to the Zwift folder."""
from __future__ import annotations

import math
import ntpath
import os
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import Optional

from .planner import Segment, Session

_NAME_RE = re.compile(r"(<name>)(.*?)(</name>)", re.DOTALL)

# A plan workout's date. Anything else is not a date, whatever produced it.
_ISO_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# A stored workout is local input, but profile publication must not turn an
# unexpectedly large or malformed XML value into an unbounded cloud object.
_PROFILE_ZWO_MAX_BYTES = 256 * 1024
_PROFILE_ZWO_MAX_SEGMENTS = 256
_PROFILE_ZWO_MAX_DURATION_S = 24 * 60 * 60
_PROFILE_ZWO_MAX_REPEATS = 256


def _xml_local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _xml_attr(element: ET.Element, name: str) -> Optional[str]:
    wanted = name.lower()
    for key, value in element.attrib.items():
        if key.lower() == wanted:
            return value
    return None


def _xml_int(element: ET.Element, name: str, *, minimum: int = 0) -> Optional[int]:
    raw = _xml_attr(element, name)
    try:
        value = int(raw or "")
    except (TypeError, ValueError, OverflowError):
        return None
    return value if minimum <= value <= _PROFILE_ZWO_MAX_DURATION_S else None


def _xml_float(element: ET.Element, name: str) -> Optional[float]:
    raw = _xml_attr(element, name)
    try:
        value = float(raw or "")
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 5 else None


def _xml_text(element: ET.Element) -> Optional[str]:
    for child in element:
        if _xml_local_name(child.tag) != "textevent":
            continue
        message = _xml_attr(child, "message")
        if message:
            return message[:160]
    return None


def session_from_zwo(
    zwo_str: str,
    *,
    name: Optional[str] = None,
    workout_type: str = "workout",
) -> Optional[Session]:
    """Read the supported prescription shape without retaining its XML.

    The cloud publisher needs the same segments that the desktop uses for its
    graph, but the XML itself is a local export format and must never cross the
    sync boundary.  Invalid or unsupported workouts return ``None`` so the
    caller can publish the ordinary calendar row without a misleading partial
    profile.
    """
    if not isinstance(zwo_str, str):
        return None
    try:
        if len(zwo_str.encode("utf-8")) > _PROFILE_ZWO_MAX_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    try:
        root = ET.fromstring(zwo_str)
    except (ET.ParseError, TypeError, ValueError):
        return None

    workout = next(
        (child for child in root if _xml_local_name(child.tag) == "workout"),
        None,
    )
    if workout is None:
        return None

    segments: list[Segment] = []
    for element in workout:
        if len(segments) >= _PROFILE_ZWO_MAX_SEGMENTS:
            return None
        kind = _xml_local_name(element.tag)
        text = _xml_text(element)
        if kind in {"warmup", "cooldown", "ramp"}:
            duration = _xml_int(element, "duration", minimum=1)
            low = _xml_float(element, "powerlow")
            high = _xml_float(element, "powerhigh")
            if duration is None or low is None or high is None:
                return None
            segment = Segment(
                kind=kind,
                duration=duration,
                power_low=low,
                power_high=high,
                text=text,
            )
        elif kind == "steadystate":
            duration = _xml_int(element, "duration", minimum=1)
            power = _xml_float(element, "power")
            if duration is None or power is None:
                return None
            segment = Segment(
                kind=kind, duration=duration, power=power, text=text,
            )
        elif kind == "intervalst":
            repeat = _xml_int(element, "repeat", minimum=1)
            on_duration = _xml_int(element, "onduration", minimum=1)
            off_duration = _xml_int(element, "offduration")
            on_power = _xml_float(element, "onpower")
            off_power = _xml_float(element, "offpower")
            if (
                repeat is None or repeat > _PROFILE_ZWO_MAX_REPEATS
                or on_duration is None or off_duration is None
                or on_power is None or off_power is None
            ):
                return None
            duration = repeat * (on_duration + off_duration)
            if duration > _PROFILE_ZWO_MAX_DURATION_S:
                return None
            segment = Segment(
                kind="intervals", duration=duration, repeat=repeat,
                on_duration=on_duration, off_duration=off_duration,
                on_power=on_power, off_power=off_power, text=text,
            )
        elif kind == "freeride":
            duration = _xml_int(element, "duration", minimum=1)
            if duration is None:
                return None
            segment = Segment(kind="freeride", duration=duration, text=text)
        else:
            # A partial parse would render a graph that does not describe the
            # exported workout, so unknown ZWO elements fail closed.
            return None
        segments.append(segment)

    if not segments or sum(segment.duration for segment in segments) > _PROFILE_ZWO_MAX_DURATION_S:
        return None
    stored_name = next(
        (child.text for child in root if _xml_local_name(child.tag) == "name"),
        None,
    )
    description = next(
        (child.text for child in root if _xml_local_name(child.tag) == "description"),
        "",
    ) or ""
    return Session(
        name=name or stored_name or "Workout",
        description=description[:160],
        workout_type=workout_type or "workout",
        segments=segments,
    )


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


def _safe_component(value: str) -> str:
    """Reduce a string to characters that can only ever be part of ONE name.

    Every separator - ``/``, ``\\``, a Windows drive colon - becomes ``_``, so
    whatever comes out cannot address anything but a file in the directory it
    is joined onto.
    """
    return "".join(c if (c.isalnum() or c in " -_.") else "_" for c in value).strip()


def plan_filename(date_iso: str, name: str) -> str:
    """Date-led, filesystem-safe .zwo filename, e.g. '2026-07-07 VO2 5x4.zwo'.

    BOTH halves are sanitised. The date used to be interpolated raw, which was
    safe only by accident: every caller read it from a plan row that had been
    through ``_dt.date.fromisoformat``. It is now also built from an export
    manifest that crossed a network, where a ``date`` of ``/etc/cron.d/x`` or
    ``../../../.config/autostart/pwn`` turned a workout export into a write to
    an arbitrary absolute path - no ``..`` even required. A value that is not a
    bare ``YYYY-MM-DD`` is not a date, so it goes through the same character
    filter as the name and stays one path component.
    """
    raw_date = str(date_iso or "")
    date = raw_date if _ISO_DATE_RE.match(raw_date) else (
        _safe_component(raw_date) or "undated"
    )
    safe = _safe_component(str(name or "")) or "workout"
    return f"{date} {safe}.zwo"


def _bare_name(filename: str) -> bool:
    """True when ``filename`` names a file and cannot leave the folder it joins.

    Checked with BOTH path flavours on purpose: a name that is inert on the OS
    that produced it must not become traversal on the OS that consumes it, and
    in a server/client install those are routinely not the same machine.
    """
    return (
        bool(filename)
        and filename not in (".", "..")
        and os.path.basename(filename) == filename
        and ntpath.basename(filename) == filename
        and not os.path.isabs(filename)
        and not ntpath.isabs(filename)
    )


def write_plan_to_zwift(
    workouts: "list[dict]",
    zwift_id: "str | None",
    workouts_override: "str | None" = None,
) -> dict:
    """Write each plan workout as a date-named .zwo into the Zwift folder.

    Each workout dict needs ``date``, ``name`` and ``zwo`` (the XML string).
    Returns {"directory": ..., "paths": [...], "count": N}.

    ``workouts_override`` is the user's STORED ``user_settings.workouts_dir``
    and nothing else. Do not pass a directory that paths.resolve_export_dir()
    already returned: this parameter is the untrusted submitted-path input, and
    a resolved folder fed back through it is re-judged as if the user had typed
    it - which refuses legitimate relocated (junctioned) Zwift folders. Let
    this function resolve; it runs the same resolver on the same inputs, and
    ``result["directory"]`` is what the caller's own resolve returned.

    Raises paths.ExportTargetUnavailable when there is no confined directory to
    write into (see paths.workouts_dir): an escaping stored workouts_dir
    (``reason == "blocked"``), several Zwift player folders to choose between
    ("choose"), or none at all ("missing"). It is deliberately not caught here
    and there is no fallback directory - a partial or misdirected export
    reported as success is the failure this replaces. Nothing is created or
    written when it raises; the caller renders the reason.
    """
    from ..paths import workouts_dir

    target_dir = workouts_dir(zwift_id, override=workouts_override)
    os.makedirs(target_dir, exist_ok=True)
    written: "list[str]" = []
    for w in workouts:
        fname = plan_filename(w["date"], w["name"])
        # Belt and braces over plan_filename's own sanitising: confining the
        # DIRECTORY is worth nothing if the filename joined onto it can walk
        # back out, and this is the last line before open(..., "w"). Whatever a
        # future filename producer does, nothing leaves target_dir.
        if not _bare_name(fname):
            raise ValueError(f"refusing an unsafe workout filename: {fname!r}")
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

    Raises paths.ExportTargetUnavailable (``.reason`` / ``.refused``) when no
    confined target can be determined, for the same reason write_plan_to_zwift
    does: there is no fallback folder, because a folder Zwift never reads is
    not a successful export. Nothing is created or written in that case.
    """
    from ..paths import workouts_dir

    target_dir = workouts_dir(zwift_id, override=workouts_override)
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, _safe_filename(name))
    with open(path, "w", encoding="utf-8") as f:
        f.write(zwo_str)
    return path
