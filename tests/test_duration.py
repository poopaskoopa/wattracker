from __future__ import annotations

import math

import pytest

from wattracker.prescribe.duration import (
    BasisStrength,
    Recommendation,
    classify_chosen_weeks,
    recommend_weeks,
)


class _RaisingFloat(float):
    def __float__(self) -> float:
        raise RuntimeError("conversion refused")


class _RaisingStr(str):
    def strip(self, chars: str | None = None) -> str:
        raise RuntimeError("normalization refused")


@pytest.mark.parametrize(
    ("goal", "floor", "ideal", "basis"),
    [
        ("ftp", 8, 12, BasisStrength.LITERATURE),
        ("criterium", 10, 16, BasisStrength.CONVENTION),
        ("long_ride", 12, 20, BasisStrength.CONVENTION),
    ],
)
def test_goal_baselines(
    goal: str,
    floor: int,
    ideal: int,
    basis: BasisStrength,
) -> None:
    recommendation = recommend_weeks(goal)

    assert recommendation.floor_weeks == floor
    assert recommendation.ideal_weeks == ideal
    assert recommendation.basis_strength is basis
    assert recommendation.rationale


def test_ftp_rationale_is_honest_about_proxy_and_evidence_limits() -> None:
    rationale = recommend_weeks("ftp").rationale.lower()

    assert "proxy" in rationale
    assert "heterogeneous" in rationale


@pytest.mark.parametrize("goal", ["criterium", "long_ride"])
def test_convention_rationales_disclose_provenance(goal: str) -> None:
    rationale = recommend_weeks(goal).rationale.lower()

    assert "convention" in rationale
    assert "not" in rationale or "rather than" in rationale


def test_low_ctl_only_extends_the_ideal() -> None:
    baseline = recommend_weeks("ftp")

    assert recommend_weeks("ftp", ctl=45).ideal_weeks == baseline.ideal_weeks + 2
    assert recommend_weeks("ftp", ctl=20).ideal_weeks == baseline.ideal_weeks + 4
    assert recommend_weeks("ftp", ctl=20).floor_weeks == baseline.floor_weeks


def test_limited_hours_only_extend_the_ideal() -> None:
    baseline = recommend_weeks("criterium")

    assert recommend_weeks("criterium", hours_per_week=4).ideal_weeks == (
        baseline.ideal_weeks + 2
    )
    assert recommend_weeks("criterium", hours_per_week=2).ideal_weeks == (
        baseline.ideal_weeks + 4
    )
    assert recommend_weeks("criterium", hours_per_week=2).floor_weeks == (
        baseline.floor_weeks
    )


def test_modifier_signals_take_larger_extension_without_stacking() -> None:
    recommendation = recommend_weeks("long_ride", ctl=45, hours_per_week=2)

    assert recommendation.ideal_weeks == 24
    assert "larger extension rather than stacking" in recommendation.rationale


def test_modifiers_are_monotonic_and_never_shorten_the_baseline() -> None:
    ctl_ideals = [
        recommend_weeks("ftp", ctl=ctl).ideal_weeks
        for ctl in (0, 29, 30, 49, 50, 100)
    ]
    hours_ideals = [
        recommend_weeks("ftp", hours_per_week=hours).ideal_weeks
        for hours in (0, 2.9, 3, 4.9, 5, 20)
    ]

    assert ctl_ideals == sorted(ctl_ideals, reverse=True)
    assert hours_ideals == sorted(hours_ideals, reverse=True)
    assert min(ctl_ideals + hours_ideals) >= recommend_weeks("ftp").ideal_weeks


@pytest.mark.parametrize(
    ("ctl", "hours"),
    [
        (None, None),
        ("low", "many"),
        (object(), []),
        (True, False),
        (math.nan, math.inf),
        (-math.inf, {"hours": 4}),
        pytest.param(10**10000, 10**10000, id="huge-integers"),
        (_RaisingFloat(1), _RaisingFloat(1)),
    ],
)
def test_malformed_optional_inputs_are_ignored(ctl: object, hours: object) -> None:
    assert recommend_weeks("ftp", ctl, hours) == recommend_weeks("ftp")


@pytest.mark.parametrize(
    "goal",
    ["unknown", "", None, object(), _RaisingStr("ftp")],
)
def test_unknown_or_malformed_goal_uses_sane_default(goal: object) -> None:
    recommendation = recommend_weeks(goal)

    assert recommendation == Recommendation(
        ideal_weeks=12,
        floor_weeks=8,
        rationale=recommendation.rationale,
        basis_strength=BasisStrength.CONVENTION,
    )
    assert "not recognized" in recommendation.rationale


def test_goal_key_is_case_and_whitespace_tolerant() -> None:
    assert recommend_weeks(" FTP ") == recommend_weeks("ftp")


@pytest.mark.parametrize(
    ("chosen", "expected"),
    [(7, "short"), (8, "ideal"), (12, "ideal"), (13, "over-long")],
)
def test_chosen_length_classification(chosen: int, expected: str) -> None:
    assert classify_chosen_weeks(chosen, recommend_weeks("ftp")) == expected


def test_recommendation_is_frozen() -> None:
    recommendation = recommend_weeks("ftp")

    with pytest.raises(AttributeError):
        recommendation.ideal_weeks = 20  # type: ignore[misc]
