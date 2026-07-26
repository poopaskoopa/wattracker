"""One presentation shape for a Session's segments, shared by every consumer.

The ride preview and the calendar's workout modal both describe the same
sessions, and both formatted them independently. That duplication is what let a
sprint render as "Max effort - no target" in one place and "0% - 0 W" twelve
times in the other, for the identical workout: the freeride case was handled in
one formatter and forgotten in the other, and the calendar's template filled the
resulting nulls with zeros.

So there is exactly one function that turns a Session into rows, and exactly one
row shape. A row carries both the raw prescription (kind, %FTP fractions - what
the calendar renders as percentages) and the presentation the web layer needs
(a label, watts, and the ``free`` flag), so no consumer has to re-derive either.

``free`` is the important one: it marks a block with NO prescribed power - a
maximal effort the rider drives. Its watt fields are all None, deliberately, and
a renderer must show the effort rather than substituting a zero.
"""
from __future__ import annotations

from typing import List, Optional

from .planner import (
    SPRINT_LOAD_RATIO_DEFAULT,
    VO2_RATIO_DEFAULT,
    Segment,
    Session,
    sprint_load_ratio,
    vo2_target,
)

# Labels for the block kinds that carry no interval structure.
_LABELS = {
    "warmup": "Warmup ramp",
    "cooldown": "Cooldown",
    "steadystate": "Steady block",
    "ramp": "Ramp",
    "freeride": "Max effort - no target",
}


def watts(fraction: Optional[float], ftp: float) -> Optional[int]:
    """A %FTP fraction as whole watts, or None when there is no target."""
    if fraction is None:
        return None
    return int(round(float(fraction) * ftp))


def _interval_label(seg: Segment, fmt_clock) -> str:
    on_s = int(seg.on_duration or 0)
    off_s = int(seg.off_duration or 0)
    return f"{seg.repeat} x {fmt_clock(on_s)} on / {fmt_clock(off_s)} easy"


def fmt_clock(seconds: int) -> str:
    """'45s' / '4min' / '4min 30s' - durations as a rider says them."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}min" if not secs else f"{minutes}min {secs}s"


def segment_rows(session: Session, ftp: float) -> List[dict]:
    """Every segment of ``session`` as one presentation row.

    Keys: everything ``Segment.to_dict`` stores (kind, duration, the %FTP
    fractions, text), plus ``duration_s`` (an alias kept because both templates
    read it), ``label``, ``free``, and the watt equivalents ``watts``,
    ``watts_low``, ``watts_high``, ``watts_on``, ``watts_off``.

    Every watt field is None when the corresponding fraction is absent. A
    consumer that substitutes 0 for None is asserting a zero-watt target, which
    for a ``free`` row is exactly wrong.
    """
    rows: List[dict] = []
    for seg in session.segments:
        row = seg.to_dict()
        row["duration_s"] = seg.duration
        row["free"] = seg.kind == "freeride"
        if seg.kind == "intervals" and seg.repeat:
            row["label"] = _interval_label(seg, fmt_clock)
        else:
            row["label"] = _LABELS.get(seg.kind, seg.kind)

        # Every watt field is the LITERAL conversion of its own fraction, in
        # the order the prescription stores it - a cooldown really does ramp
        # from high to low, and a renderer that wants an ascending range is
        # the thing that should sort it. A free row has no fractions at all,
        # so all of these are None; substituting 0 asserts a zero-watt target.
        row["watts"] = watts(seg.power, ftp)
        row["watts_low"] = watts(seg.power_low, ftp)
        row["watts_high"] = watts(seg.power_high, ftp)
        row["watts_on"] = watts(seg.on_power, ftp)
        row["watts_off"] = watts(seg.off_power, ftp)
        rows.append(row)
    return rows


def target_status(profile, computed_at: Optional[str] = None) -> dict:
    """Is this rider's prescription personalised, and from when?

    Surfacing this is not decoration. A profile that is never computed - the
    case with the background sweep disabled - silently prescribes population
    constants, and nothing anywhere said so; that silence is exactly what let a
    stale-profile bug survive. ``computed_at`` makes the basis of every target
    inspectable by the person it is prescribed for.
    """
    vo2 = vo2_target(profile)
    sprint = _measured_sprint(profile)
    return {
        "computed_at": computed_at,
        "computed": computed_at is not None,
        "personalised": bool(vo2 is not None or sprint is not None),
        "vo2_ratio": getattr(profile, "vo2_ratio", None),
        "sprint_ratio": getattr(profile, "sprint_ratio", None),
        "vo2_target": vo2,
        "vo2_default": VO2_RATIO_DEFAULT,
        "sprint_load": sprint_load_ratio(profile),
        "sprint_default": SPRINT_LOAD_RATIO_DEFAULT,
    }


def _measured_sprint(profile) -> Optional[float]:
    """The rider's own sprint figure, or None when it is the population one."""
    ratio = sprint_load_ratio(profile)
    if getattr(profile, "sprint_ratio", None) is None:
        return None
    return ratio
