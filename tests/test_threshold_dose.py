"""Threshold dose and the missing trailing recovery.

Two defects found in a real 60-minute threshold session:

* ``_threshold`` dropped reps before shortening the interval, so an hour became
  2 x 13min = 26 minutes in zone - a sweet-spot dose wearing a threshold label.
  Sports-science norm for a 60-minute threshold session is 35-45 minutes in
  zone (2x20, 3x15, 3x12, 4x10).
* every interval segment emitted a TRAILING recovery, and ``_finish`` appended
  the cooldown straight after it, so the ride ended 5min @55% + 10min cooldown:
  a quarter of the session easy, one part of it "recovering" into a cooldown.
"""
import pytest

from wattracker.prescribe import zwo
from wattracker.prescribe.planner import (
    MEASUREMENT_TYPES, VARIANTS, build_workout)


def _kinds_and_durations(session):
    return [(s.kind, s.duration) for s in session.segments]


def _time_in_zone(session, power, tol=1e-9):
    """Seconds spent at (or above) ``power`` across every segment shape."""
    total = 0
    for s in session.segments:
        if s.kind == "intervals" and s.repeat:
            if (s.on_power or 0.0) >= power - tol:
                total += s.repeat * (s.on_duration or 0)
            if (s.off_power or 0.0) >= power - tol:
                total += s.repeat * (s.off_duration or 0)
        elif s.kind == "steadystate" and (s.power or 0.0) >= power - tol:
            total += s.duration
    return total


# ------------------------------------------------------------ the 60min dose
def test_threshold_hour_is_three_by_twelve_not_two_by_thirteen():
    s = build_workout("threshold", 60)
    assert _kinds_and_durations(s) == [
        ("warmup", 600),
        ("intervals", 1920),      # 2 x (720 work + 240 recovery)
        ("steadystate", 720),     # third rep, no recovery after it
        ("cooldown", 360),
    ]
    work = s.segments[1]
    assert (work.repeat, work.on_duration, work.off_duration) == (2, 720, 240)
    assert work.on_power == 0.93 and work.off_power == 0.55
    assert s.segments[2].power == 0.93
    # 3 x 12min = 36 minutes at threshold, inside the 35-45min norm.
    assert _time_in_zone(s, 0.93) == 2160
    assert "3 x 12min" in s.description


def test_threshold_two_by_twenty_hour_is_two_by_nineteen():
    s = build_workout("threshold", 60, "two_by_twenty")
    assert _kinds_and_durations(s) == [
        ("warmup", 600),
        ("intervals", 1380),      # 1 x (1140 work + 240 recovery)
        ("steadystate", 1140),    # second rep, no recovery after it
        ("cooldown", 480),
    ]
    work = s.segments[1]
    assert (work.repeat, work.on_duration, work.off_duration) == (1, 1140, 240)
    assert work.on_power == 0.91
    # 2 x 19min = 38 minutes at threshold.
    assert _time_in_zone(s, 0.91) == 2280
    assert "2 x 19min" in s.description


@pytest.mark.parametrize("variant", ["classic", "two_by_twenty"])
def test_threshold_hour_delivers_35_to_45_min_in_zone(variant):
    s = build_workout("threshold", 60, variant)
    work = next(seg for seg in s.segments if seg.kind == "intervals")
    tiz = _time_in_zone(s, work.on_power)
    assert 35 * 60 <= tiz <= 45 * 60, (variant, tiz)


def test_threshold_shortens_the_interval_before_dropping_a_rep():
    # 55 minutes still buys three reps, just shorter ones (3 x 10min) - the old
    # builder dropped to two reps first and gave the time back as cooldown.
    s = build_workout("threshold", 55)
    work = next(seg for seg in s.segments if seg.kind == "intervals")
    assert work.repeat + 1 == 3
    assert work.on_duration >= 600


def test_threshold_drops_to_two_reps_only_when_nothing_else_fits():
    s = build_workout("threshold", 30)
    work = next(seg for seg in s.segments if seg.kind == "intervals")
    assert work.repeat + 1 == 2
    assert s.total_duration() == 1800


# ------------------------------------------------- no trailing recovery
# Builders whose recovery is a pure rest between efforts. Excluded on purpose:
# over-unders and sweet-spot-with-surges (the "off" is the harder half of the
# stimulus, not a rest), 30/30s (the recovery is the interval), the descending
# ladder (already ends on work) and the endurance/tempo/sprint builders, which
# are outside this change.
_NO_TRAILING_RECOVERY = [
    ("threshold", "classic"),
    ("threshold", "two_by_twenty"),
    ("sweet_spot", "classic"),
    ("sweet_spot", "long_blocks"),
    ("vo2max", "classic"),
    ("vo2max", "long_intervals"),
]


@pytest.mark.parametrize("kind,variant", _NO_TRAILING_RECOVERY)
@pytest.mark.parametrize("minutes", [30, 60, 120])
def test_session_ends_on_work_then_cooldown(kind, variant, minutes):
    s = build_workout(kind, minutes, variant)
    kinds = [seg.kind for seg in s.segments]
    # Nothing between the last effort and the cooldown.
    assert kinds[-1] == "cooldown"
    last_work = s.segments[-2]
    assert last_work.kind == "steadystate"
    intervals = [seg for seg in s.segments if seg.kind == "intervals"]
    if intervals:
        assert last_work.power == intervals[0].on_power
        assert last_work.duration == intervals[0].on_duration
        # The repeated block carries one fewer rep than prescribed.
        assert last_work is s.segments[s.segments.index(intervals[0]) + 1]


@pytest.mark.parametrize("kind,variant", _NO_TRAILING_RECOVERY)
def test_work_time_is_reps_on_plus_reps_minus_one_off(kind, variant):
    s = build_workout(kind, 60, variant)
    intervals = next(seg for seg in s.segments if seg.kind == "intervals")
    reps = intervals.repeat + 1
    on, off = intervals.on_duration, intervals.off_duration
    final = s.segments[s.segments.index(intervals) + 1]
    assert intervals.duration + final.duration == reps * on + (reps - 1) * off


def test_single_rep_emits_no_intervals_segment():
    from wattracker.prescribe.planner import _interval_block

    assert [s.kind for s in _interval_block(1, 600, 240, 0.93, 0.55, "x")] == [
        "steadystate"
    ]
    assert [s.kind for s in _interval_block(2, 600, 240, 0.93, 0.55, "x")] == [
        "intervals", "steadystate"
    ]


def test_final_rep_renders_as_a_steadystate_block_in_the_zwo():
    xml = zwo.zwo_string(build_workout("threshold", 60))
    assert '<SteadyState Duration="720" Power="0.93"' in xml
    assert 'Repeat="2"' in xml and 'OnDuration="720"' in xml


# ------------------------------------------------------------ duration budget
@pytest.mark.parametrize("seconds", [1800, 2700, 3600, 5400, 7200])
def test_every_builder_sums_to_the_requested_duration(seconds):
    for kind, variants in VARIANTS.items():
        for variant in variants:
            s = build_workout(kind, seconds // 60, variant)
            if kind in MEASUREMENT_TYPES:
                # The requested duration bounds a measurement protocol rather
                # than setting it: a ramp test ends when the rider fails.
                assert 0 < s.total_duration() <= seconds, (kind, variant)
            else:
                assert s.total_duration() == seconds, (kind, variant)
            assert all(seg.duration > 0 for seg in s.segments), (kind, variant)
