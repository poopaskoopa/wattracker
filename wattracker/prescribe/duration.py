"""Conservative plan-length recommendations for common cycling goals."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from numbers import Real
from typing import Literal


class BasisStrength(str, Enum):
    """How directly the baseline is supported by published evidence."""

    LITERATURE = "LITERATURE"
    CONVENTION = "CONVENTION"


@dataclass(frozen=True)
class Recommendation:
    """A plan-length recommendation and the provenance behind it."""

    ideal_weeks: int
    floor_weeks: int
    rationale: str
    basis_strength: BasisStrength


@dataclass(frozen=True)
class _Baseline:
    floor_weeks: int
    ideal_weeks: int
    rationale: str
    basis_strength: BasisStrength


_BASELINES: dict[str, _Baseline] = {
    "ftp": _Baseline(
        floor_weeks=8,
        ideal_weeks=12,
        basis_strength=BasisStrength.LITERATURE,
        rationale=(
            "The 8-week floor and 12-week ideal reflect the common duration of "
            "structured aerobic-training studies. FTP is only a practical proxy "
            "for threshold performance, however, and study populations, protocols, "
            "and responses are heterogeneous, so this is a planning range rather "
            "than a promised adaptation timeline."
        ),
    ),
    "criterium": _Baseline(
        floor_weeks=10,
        ideal_weeks=16,
        basis_strength=BasisStrength.CONVENTION,
        rationale=(
            "The 10-week floor and 16-week ideal are coaching convention, not a "
            "goal-specific result established by a single literature base. The "
            "range leaves time to build aerobic support before adding repeated "
            "high-power efforts and race-specific work."
        ),
    ),
    "long_ride": _Baseline(
        floor_weeks=12,
        ideal_weeks=20,
        basis_strength=BasisStrength.CONVENTION,
        rationale=(
            "The 12-week floor and 20-week ideal are coaching convention rather "
            "than a validated universal prescription. The longer runway supports "
            "gradual volume, fueling, and fatigue-resistance practice while limiting "
            "abrupt increases in long-ride load."
        ),
    ),
}

_DEFAULT = _Baseline(
    floor_weeks=8,
    ideal_weeks=12,
    basis_strength=BasisStrength.CONVENTION,
    rationale=(
        "The goal key is not recognized, so this uses a conservative general-prep "
        "convention: an 8-week floor and 12-week ideal. It is a neutral planning "
        "default, not goal-specific evidence."
    ),
)


def _finite_number(value: object) -> float | None:
    """Return a usable finite number while treating bad optional data as absent."""
    # bool is technically an int, but interpreting True as 1 CTL or hour/week
    # would turn a serialization mistake into a large recommendation change.
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except Exception:
        # Real implementations can still reject conversion, and extremely large
        # integers overflow float. Optional profile data must never gate planning.
        return None
    return number if math.isfinite(number) else None


def _extra_weeks(ctl: object, hours_per_week: object) -> tuple[int, list[str]]:
    """Translate limited preparation capacity into one discrete runway extension."""
    ctl_value = _finite_number(ctl)
    hours_value = _finite_number(hours_per_week)
    ctl_extra = 4 if ctl_value is not None and ctl_value < 30 else (
        2 if ctl_value is not None and ctl_value < 50 else 0
    )
    hours_extra = 4 if hours_value is not None and hours_value < 3 else (
        2 if hours_value is not None and hours_value < 5 else 0
    )

    # Low CTL and limited weekly time are overlapping signals that more calendar
    # runway is useful. Taking the larger adjustment avoids double-counting the
    # same constraint, and modifiers can never shorten the goal baseline.
    extra = max(ctl_extra, hours_extra)
    reasons: list[str] = []
    if ctl_extra:
        reasons.append(
            f"CTL indicates a {'low' if ctl_extra == 4 else 'moderate'} "
            f"starting training load (+{ctl_extra} weeks)"
        )
    if hours_extra:
        reasons.append(
            f"weekly training time is {'under 3' if hours_extra == 4 else 'under 5'} "
            f"hours (+{hours_extra} weeks)"
        )
    return extra, reasons


def recommend_weeks(
    goal_key: object,
    ctl: object = None,
    hours_per_week: object = None,
) -> Recommendation:
    """Recommend a plan length without rejecting incomplete profile data."""
    try:
        candidate = goal_key.strip().lower() if isinstance(goal_key, str) else ""
        # A str subclass can return an arbitrary, even unhashable, object from
        # normalization. Only a plain string is safe as a baseline lookup key.
        normalized = candidate if type(candidate) is str else ""
    except Exception:
        # A hostile str subclass is malformed input, equivalent to an unknown key.
        normalized = ""
    baseline = _BASELINES.get(normalized, _DEFAULT)
    extra, reasons = _extra_weeks(ctl, hours_per_week)
    rationale = baseline.rationale
    if reasons:
        rationale += (
            " The ideal is extended by "
            f"{extra} weeks because "
            + " and ".join(reasons)
            + "; overlapping signals use the larger extension rather than stacking."
        )
    return Recommendation(
        ideal_weeks=baseline.ideal_weeks + extra,
        floor_weeks=baseline.floor_weeks,
        rationale=rationale,
        basis_strength=baseline.basis_strength,
    )


def classify_chosen_weeks(
    chosen_weeks: int,
    recommendation: Recommendation,
) -> Literal["short", "ideal", "over-long"]:
    """Describe a user's choice without changing or rejecting that choice."""
    if chosen_weeks < recommendation.floor_weeks:
        return "short"
    if chosen_weeks <= recommendation.ideal_weeks:
        return "ideal"
    return "over-long"
