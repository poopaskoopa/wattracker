"""Tests for NP / IF / TSS and the FTP estimator."""
import numpy as np
import pytest

from wattracker.metrics import power


def test_np_constant_equals_mean():
    # Constant power -> NP equals that power.
    stream = [200.0] * 3600
    assert power.normalized_power(stream) == pytest.approx(200.0, abs=1e-6)


def test_np_short_series_falls_back_to_mean():
    stream = [100.0, 200.0, 300.0]  # < 30 samples
    assert power.normalized_power(stream) == pytest.approx(200.0)


def test_np_hand_computed():
    # 60 samples: first 30 at 100W, next 30 at 300W.
    # 30s rolling means range from 100 -> 300 as the window slides.
    stream = [100.0] * 30 + [300.0] * 30
    roll = power.rolling_mean(stream, 30)
    expected = float(np.power(np.mean(np.power(roll, 4)), 0.25))
    assert power.normalized_power(stream) == pytest.approx(expected)


def test_if():
    assert power.intensity_factor(250.0, 250.0) == pytest.approx(1.0)
    assert power.intensity_factor(200.0, 250.0) == pytest.approx(0.8)


def test_one_hour_at_ftp_is_100_tss():
    ftp = 250.0
    stream = [ftp] * 3600  # 1 hour exactly at FTP
    tss = power.tss_from_stream(stream, ftp)
    assert tss == pytest.approx(100.0, abs=0.5)


def test_tss_formula_direct():
    # 30 min (1800s) at NP=FTP -> IF=1 -> TSS = 50.
    assert power.training_stress_score(1800, 250.0, 250.0) == pytest.approx(50.0)


def test_estimate_ftp_recovers_20min_effort():
    # A 20-min (1200s) effort at 300W -> FTP = 300 * 0.95 = 285.
    effort = [300.0] * 1200
    est = power.estimate_ftp([effort])
    assert est == pytest.approx(285.0, abs=1e-6)


def test_estimate_ftp_recognizes_zwift_ramp_test():
    # Five rising one-minute steps after a short warm-up; the final step is
    # 304W, so Zwift's 75% rule gives 228W.
    stream = [100.0] * 180 + [w for w in (200.0, 220.0, 240.0, 260.0, 284.0, 304.0) for _ in range(60)]
    assert power.estimate_ftp([stream]) == pytest.approx(228.0)


def test_estimate_ftp_rejects_ordinary_one_minute_intervals():
    stream = [100.0] * 180 + [w for w in (200.0, 300.0, 200.0, 300.0, 200.0, 300.0) for _ in range(60)]
    assert power.estimate_ftp([stream]) == 0.0


def test_estimate_ftp_rejects_ramp_embedded_in_long_ride():
    ramp = [100.0] * 180 + [w for w in (200.0, 220.0, 240.0, 260.0, 284.0, 304.0) for _ in range(60)]
    long_ride = ramp + [120.0] * (45 * 60)
    assert len(long_ride) > 45 * 60
    assert power.ramp_test_ftp_candidate(long_ride) == 0.0
    assert power.estimate_ftp([long_ride]) < 228.0


@pytest.mark.parametrize("corrupt", [
    42,
    "300",
    b"300",
    bytearray(b"300"),
    {"sample": 300},
])
def test_estimate_ftp_skips_corrupt_dict_power_and_uses_valid_effort(corrupt):
    activities = [
        {"start_time": "2026-06-01T10:00:00", "streams": {"power": corrupt}},
        {
            "start_time": "2026-06-02T10:00:00",
            "streams": {"power": [300.0] * 1200},
        },
    ]
    assert power.estimate_ftp(activities) == pytest.approx(285.0)


def test_estimate_ftp_preserves_tuple_generator_and_numpy_raw_streams():
    assert power.estimate_ftp([(300.0,) * 1200]) == pytest.approx(285.0)
    assert power.estimate_ftp((iter([300.0] * 1200),)) == pytest.approx(285.0)
    assert power.estimate_ftp([np.full(1200, 300.0)]) == pytest.approx(285.0)


def test_estimate_ftp_override_wins():
    assert power.estimate_ftp([[300.0] * 1200], override=250.0) == 250.0


def test_estimate_ftp_recent_effort_dominates_decayed_old():
    # A recent 300W effort beats a 400W effort from 100 days ago once the latter
    # is detraining-decayed across the ~95-day idle gap between them.
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    recent = {
        "start_time": (now - dt.timedelta(days=5)).isoformat(),
        "streams": {"power": [300.0] * 1200},
    }
    old = {
        "start_time": (now - dt.timedelta(days=100)).isoformat(),
        "streams": {"power": [400.0] * 1200},
    }
    est = power.estimate_ftp([recent, old], now=now)
    # Recent wins: 300 * factor(idle~0, active~5) * 0.95. Those 5 days were all
    # training days, so they are charged staleness only - under 0.1% - and the
    # result sits essentially on the undecayed 285. The recent-evidence floor
    # sees the same effort decayed by the same factor, so on a plain 20-minute
    # effort it agrees exactly rather than cancelling the decay.
    assert est == pytest.approx(285.0 * power.detraining_factor(0.0, 5.0), abs=0.1)
    assert est == pytest.approx(284.8, abs=0.2)
    assert est > 285.0 * 0.999


# ----------------------------------------------- gap-aware detraining model
def test_idle_active_split_continuous_training_is_all_active():
    # An effort followed by riding every 5 days accrues no idle excess: the
    # whole span is "active", so decay stays minimal (this is the v1 bug case).
    import datetime as dt

    t = dt.datetime(2026, 1, 1, 10, 0)
    days = [t + dt.timedelta(days=5 * i) for i in range(8)]  # t .. t+35
    anchor = t + dt.timedelta(days=35)
    idle, active = power._idle_active_days(t, anchor, days)
    assert idle == pytest.approx(0.0, abs=1e-9)
    assert active == pytest.approx(35.0, abs=1e-9)


def test_idle_active_split_lone_effort_then_layoff():
    # A single effort with no rides until the anchor: the whole gap past the
    # grace window is idle excess.
    import datetime as dt

    t = dt.datetime(2026, 1, 1, 10, 0)
    anchor = t + dt.timedelta(days=40)
    idle, active = power._idle_active_days(t, anchor, [t])
    assert idle == pytest.approx(40 - power.FTP_DECAY_GRACE_DAYS, abs=1e-9)
    assert active == pytest.approx(power.FTP_DECAY_GRACE_DAYS, abs=1e-9)


def test_detraining_factor_no_decay_when_no_idle_no_active():
    assert power.detraining_factor(0.0, 0.0) == 1.0


def test_detraining_factor_active_days_are_staleness_not_detraining():
    # Days the rider spent TRAINING must not be charged detraining. The active
    # term is an evidence-staleness discount only, calibrated in years:
    # FTP_DECAY_ACTIVE_ANNUAL_LOSS per year of unbroken training, exactly.
    year = power.detraining_factor(0.0, 365.0)
    assert year == pytest.approx(1.0 - power.FTP_DECAY_ACTIVE_ANNUAL_LOSS, abs=1e-9)
    # Regression on the reported bug: the old tau=1440 charged a rider who never
    # stopped training 22% a year, a detraining-sized loss for training.
    assert 1.0 - year < 0.06
    # A training month is inside the noise of the underlying 20-minute number.
    assert power.detraining_factor(0.0, 30.0) > 0.995
    # A season of unbroken training still costs under 3%.
    assert power.detraining_factor(0.0, 180.0) > 0.97


def test_detraining_factor_active_term_still_retires_ancient_efforts():
    # It is not simply removed: the recent-effort floor only looks back 42 days
    # and can never lower the estimate, so without an active term one ancient
    # hard effort would peg FTP forever. Three years of easy-only riding must
    # visibly discount it - but by staleness magnitudes, not detraining ones.
    three_years = power.detraining_factor(0.0, 3.0 * 365.0)
    assert 0.10 < 1.0 - three_years < 0.20


def test_detraining_factor_idle_dominates_active_by_orders_of_magnitude():
    # The model's premise: being OFF the bike is what costs fitness. A single
    # idle day past the grace window must outweigh a long stretch of training.
    assert power.detraining_factor(1.0, 0.0) < power.detraining_factor(0.0, 25.0)


def test_detraining_factor_monotonic_in_both_terms():
    base = power.detraining_factor(20.0, 20.0)
    assert power.detraining_factor(40.0, 20.0) < base   # more idle -> lower
    assert power.detraining_factor(20.0, 40.0) < base   # more active -> lower
    assert power.detraining_factor(10.0, 20.0) > base   # less idle -> higher


def test_detraining_factor_about_088_before_six_week_stop():
    # Effort ridden immediately before a six-week full stop, evaluated at return:
    # idle excess ~28, active ~14 -> ~0.88.
    assert power.detraining_factor(28.0, 14.0) == pytest.approx(0.88, abs=0.01)


def test_estimate_ftp_decays_over_layoff_not_collapse():
    # Regression for the reported live-data bug: a ~216W best-20 effort at
    # the START of a six-week break, with regular training before it and easy
    # return rides after, must decay by ~0.87 (only the 44-day gap counts, NOT
    # the weeks of active training) -> ~179, NOT collapse to ~143/159.
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    # Regular training every 4 days ending just before the break start.
    train = [
        {
            "start_time": (now - dt.timedelta(days=d)).isoformat(),
            "streams": {"power": [180.0] * 1200},
        }
        for d in range(93, 49, -4)  # 93, 89, ... 53
    ]
    hard = {
        "start_time": (now - dt.timedelta(days=49)).isoformat(),  # break start
        "streams": {"power": [216.0] * 1200},
    }
    easy = [
        {
            "start_time": (now - dt.timedelta(days=d)).isoformat(),
            "streams": {"power": [150.0] * 1200},
        }
        for d in (5, 3, 1)  # return rides after the 44-day gap
    ]
    est = power.estimate_ftp(train + [hard] + easy, now=now)
    # 216 * 0.95 = 205.2 baseline; * ~0.87 -> ~178.7.
    assert est == pytest.approx(178.7, abs=2.0)
    assert est > 170.0  # nowhere near the collapsed ~143/159


def test_estimate_ftp_continuous_training_barely_decays_old_effort():
    # The v1 bug: an effort ridden while the rider KEEPS training must not be
    # charged detraining. 250W effort 35 days ago, riding every 5 days since.
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    acts = [
        {
            "start_time": (now - dt.timedelta(days=d)).isoformat(),
            "streams": {"power": ([250.0] if d == 35 else [180.0]) * 1200},
        }
        for d in range(35, -1, -5)  # 35, 30, ... 0
    ]
    est = power.estimate_ftp(acts, now=now)
    baseline = 250.0 * 0.95  # 237.5
    # Every one of those 35 days was a training day, so the rider is charged
    # staleness only (~0.5%) -> ~236.3, far above v1's effort-age ~214 and above
    # the ~231.8 the old tau=1440 staleness term produced.
    assert est == pytest.approx(236.3, abs=0.5)
    assert est > 0.995 * baseline


def test_estimate_ftp_fresh_hard_effort_dominates_history():
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    old = {
        "start_time": (now - dt.timedelta(days=200)).isoformat(),
        "streams": {"power": [250.0] * 1200},
    }
    fresh = {
        "start_time": (now - dt.timedelta(days=1)).isoformat(),
        "streams": {"power": [320.0] * 1200},
    }
    est = power.estimate_ftp([old, fresh], now=now)
    # 320 * factor(idle~0, active~1) * 0.95 ~= 303.8.
    assert est == pytest.approx(303.8, abs=0.5)


# ------------------------------------------------- recent-evidence FTP floor
# A rider who only does structured ERG work never rides a maximal 20 minutes,
# so the decayed best-20 estimate reads 15-20% low. The floor reads FTP off the
# long efforts they DO ride (>= 13min) instead.
import datetime as _dt  # noqa: E402


def _act(now, days_ago, watts, seconds, rpe=None):
    a = {
        "start_time": (now - _dt.timedelta(days=days_ago)).isoformat(),
        "streams": {"power": [float(watts)] * seconds},
    }
    if rpe is not None:
        a["rpe"] = rpe
    return a


def _decay(acts, now, days_ago):
    """Detraining factor for an effort ``days_ago`` old against ``acts``.

    The floor decays each contribution exactly as the estimator decays each
    effort, so every expectation below carries this term rather than pretending
    a recent effort is worth its raw watts forever.
    """
    _, calendar = power._split_activities(acts)
    idle, active = power._idle_active_days(
        now - _dt.timedelta(days=days_ago), now, calendar
    )
    return power.detraining_factor(idle, active)


def test_recent_effort_floor_maps_each_duration_to_its_ftp_fraction():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    for seconds, fraction in power.FTP_FLOOR_DURATIONS:
        acts = [_act(now, 3, 200.0, seconds)]
        floor = power.recent_effort_floor(acts, now)
        assert floor == pytest.approx(200.0 * fraction * _decay(acts, now, 3))


def test_recent_effort_floor_takes_the_best_across_durations_and_activities():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    acts = [
        _act(now, 10, 200.0, 780),    # -> 184.0 before decay
        _act(now, 4, 195.0, 1800),    # -> 191.1 before decay
    ]
    assert power.recent_effort_floor(acts, now) == pytest.approx(
        195.0 * 0.98 * _decay(acts, now, 4)
    )


def test_recent_effort_floor_ignores_efforts_shorter_than_thirteen_minutes():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    # A 5-minute maximal effort says nothing about the hour and must not floor.
    assert power.recent_effort_floor([_act(now, 1, 400.0, 300)], now) == 0.0


def test_recent_effort_floor_ignores_activities_outside_the_window():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    inside_acts = [_act(now, 41, 200.0, 1200)]
    outside_acts = [_act(now, 43, 200.0, 1200)]
    inside = power.recent_effort_floor(inside_acts, now)
    outside = power.recent_effort_floor(outside_acts, now)
    assert inside == pytest.approx(190.0 * _decay(inside_acts, now, 41))
    assert outside == 0.0
    # The window is a parameter, not a constant.
    assert power.recent_effort_floor(
        outside_acts, now, window_days=60
    ) == pytest.approx(190.0 * _decay(outside_acts, now, 43))


def test_recent_effort_floor_credits_a_submaximal_rpe():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)

    def floor(rpe):
        acts = [_act(now, 2, 200.0, 1200, rpe=rpe)]
        return power.recent_effort_floor(acts, now) / (
            200.0 * 0.95 * _decay(acts, now, 2)
        )

    # rpe 6 -> +5%, rpe 4 -> +10% (the cap), rpe 1 -> still the +10% cap.
    assert floor(6) == pytest.approx(1.05)
    assert floor(4) == pytest.approx(1.10)
    assert floor(1) == pytest.approx(1.10)


def test_recent_effort_floor_does_not_adjust_maximal_or_missing_rpe():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    for rpe in (None, 8, 9, 10):
        acts = [_act(now, 2, 200.0, 1200, rpe=rpe)]
        assert power.recent_effort_floor(acts, now) == pytest.approx(
            200.0 * 0.95 * _decay(acts, now, 2)
        )


def test_recent_effort_floor_decays_when_the_rider_stops_riding():
    """The floor must not cancel detraining inside its own window.

    Same 20-minute effort 40 days ago in both cases. The rider who kept riding
    every 5 days keeps essentially all of it - that is what the floor exists
    for. The rider who did the effort and then stopped loses real watts.
    """
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    stopped = [_act(now, 40, 200.0, 1200)]
    kept = [_act(now, 40, 200.0, 1200)] + [
        _act(now, d, 120.0, 1200) for d in range(35, -1, -5)
    ]
    assert power.recent_effort_floor(stopped, now) < 0.92 * 190.0
    assert power.recent_effort_floor(kept, now) > 0.97 * 190.0


def test_recent_effort_floor_is_zero_without_now():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    assert power.recent_effort_floor([_act(now, 1, 300.0, 1200)], None) == 0.0


def test_estimate_ftp_with_now_none_ignores_the_floor():
    # The raw-stream contract is unchanged: no decay, no floor, just 0.95 x
    # best-20. A 13-minute effort at 300W would floor at 276 if it applied.
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    acts = [_act(now, 1, 300.0, 780, rpe=5)]
    assert power.estimate_ftp(acts) == 0.0
    assert power.estimate_ftp(acts, now=None) == 0.0
    mixed = acts + [_act(now, 1, 200.0, 1200)]
    assert power.estimate_ftp(mixed) == pytest.approx(190.0)


def test_estimate_floored_by_recent_effort_inside_window():
    """The live case: structured ERG work only, no maximal 20 minutes.

    A rider whose best 13-minute power is 197.8W at RPE 6, with no maximal
    20-minute effort anywhere, read ~177W before the floor existed.
    """
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    # An ERG session: 3 x 13min at ~198W with easy recoveries between them.
    stream = ([198.0] * 780 + [110.0] * 240) * 3
    acts = [{"start_time": (now - _dt.timedelta(days=1)).isoformat(),
             "rpe": 6,
             "streams": {"power": stream}}]
    decayed_only = power.estimate_ftp(acts, now=None)
    est = power.estimate_ftp(acts, now=now)
    # 197.8 * 0.92 * 1.05 = 191.0, well above the diluted best-20 estimate.
    assert est == pytest.approx(
        198.0 * 0.92 * 1.05 * _decay(acts, now, 1), abs=0.5
    )
    assert est > decayed_only * 1.05


def test_estimate_ftp_floor_never_lowers_the_estimate():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    # A genuine 20-minute test dominates; the floor agrees rather than cutting.
    acts = [_act(now, 2, 300.0, 1200), _act(now, 1, 120.0, 3600)]
    assert power.estimate_ftp(acts, now=now) == pytest.approx(
        285.0 * _decay(acts, now, 2), abs=0.5
    )


def test_estimate_ftp_accepts_a_generator_with_the_floor_active():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    acts = [_act(now, 2, 200.0, 1200)]
    assert power.estimate_ftp(iter(acts), now=now) == pytest.approx(
        190.0 * _decay(acts, now, 2)
    )


def test_split_activities_carries_rpe_through():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    efforts, _ = power._split_activities(
        [_act(now, 1, 200.0, 60, rpe=7), _act(now, 2, 200.0, 60), [1.0, 2.0]]
    )
    assert [e[2] for e in efforts] == [7, None, None]


def _synth_minutes(minutes, seed=20260814):
    """Build a per-second stream from (mean, sample sd) pairs, one per minute.

    The noise is centred and rescaled so every minute carries exactly the
    requested mean and sample standard deviation, which keeps the fixtures
    deterministic without embedding anyone's real power samples.  Samples are
    clipped at zero, so a minute whose sd reaches below zero watts (a soft
    warm-up or a coasting cooldown) ends up marginally tamer than requested.

    Note that these fixtures are harder than the rides they model: a real ramp
    rises within the minute, so a block straddling two steps still looks like a
    step, whereas here the flat per-minute means make a straddling block look
    unsteady.  The real activity behind ``_REAL_RAMP_SHAPE`` is recognized from
    a 6% steadiness allowance; the fixture needs the full 7%.
    """
    rng = np.random.default_rng(seed)
    stream = []
    for mean, sd in minutes:
        noise = rng.standard_normal(60)
        noise -= noise.mean()
        if sd > 0:
            noise *= sd / noise.std(ddof=1)
        stream.extend(np.clip(mean + noise, 0.0, None).tolist())
    return stream


# The shape of a real 26.9-minute Zwift ramp test: a soft warm-up, ten rising
# one-minute steps that get noisier as they get hard, then a ragged cooldown.
_REAL_RAMP_SHAPE = [
    (42.5, 23.4), (49.1, 9.5), (44.0, 5.0), (43.0, 45.1), (35.7, 13.7),
    (93.9, 16.4),
    (120.7, 7.6), (138.8, 7.4), (158.1, 7.8), (177.7, 10.0), (197.4, 8.6),
    (217.2, 11.6), (237.7, 16.2), (256.6, 10.7), (278.1, 10.3), (297.6, 13.7),
    (46.0, 89.3),
] + [(70.0, 5.0)] * 9


def test_ramp_candidate_survives_noisy_late_steps_and_a_cooldown():
    # Regression: the steps at 237.7 W (sd 16.2) and 297.6 W (sd 13.7) exceed a
    # flat 12 W bound, and the cooldown is wild, yet this is unambiguously a
    # ramp test.  The candidate is 75% of the top step, 297.6 W.
    stream = _synth_minutes(_REAL_RAMP_SHAPE)
    assert len(stream) == 26 * 60
    assert power.ramp_test_ftp_candidate(stream) == pytest.approx(223.2, abs=0.1)
    assert power.estimate_ftp([stream]) == pytest.approx(223.2, abs=0.1)


def test_ramp_candidate_recognized_when_followed_by_a_cooldown():
    stream = _synth_minutes(
        [(100.0, 4.0)] * 3
        + [(200.0, 6.0), (220.0, 6.0), (240.0, 6.0), (260.0, 6.0),
           (284.0, 6.0), (304.0, 6.0)]
        + [(80.0, 40.0)] * 8
    )
    assert power.ramp_test_ftp_candidate(stream) == pytest.approx(228.0, abs=0.5)


def test_ramp_candidate_tolerates_noise_at_the_start_of_the_run():
    # Noise early rather than late: the first two steps of the ramp are the
    # ragged ones, both well over the flat 12 W bound.
    stream = _synth_minutes(
        [(120.0, 5.0)] * 3
        + [(250.0, 15.0), (275.0, 15.0), (300.0, 8.0), (325.0, 8.0),
           (350.0, 8.0)]
        + [(75.0, 30.0)] * 6
    )
    assert power.ramp_test_ftp_candidate(stream) == pytest.approx(262.5, abs=0.5)


# --- Negatives.  Each is annotated with the guard that holds it, because a
# --- fixture rejected by the slope band pins nothing about the others.

def test_ramp_candidate_rejects_a_climb_over_a_col_then_a_descent():
    # Held by the steadiness bound alone.  Everything else about this looks
    # like a ramp: six rising 25 W steps from a lower lead-in, then a descent
    # that collapses the power.  Only the ragged 300 W step (sd 22.5, 7.5% of
    # its own power) rejects it, so loosening the bound past 7% accepts it.
    stream = _synth_minutes(
        [(175.0, 8.0)] * 4
        + [(200.0, 8.0), (225.0, 8.0), (250.0, 8.0), (275.0, 8.0),
           (300.0, 22.5), (325.0, 8.0)]
        + [(60.0, 20.0)] * 10
    )
    assert power.ramp_test_ftp_candidate(stream) == 0.0


def test_ramp_candidate_rejects_group_ride_surges_then_sitting_up():
    # Held by the steadiness bound alone, at a lower power than the col above,
    # so the two together pin the bound's slope and not just one point on it.
    stream = _synth_minutes(
        [(120.0, 6.0)] * 4
        + [(160.0, 9.0), (180.0, 9.0), (200.0, 9.0), (220.0, 9.0),
           (240.0, 18.0), (260.0, 9.0)]
        + [(90.0, 25.0)] * 10
    )
    assert power.ramp_test_ftp_candidate(stream) == 0.0


def test_ramp_candidate_rejects_a_soft_pedal_before_a_climb():
    # Held by the collapse rule.  A rider who soft-pedals at 140 W and then
    # climbs at 12 W/min - inside the ramp slope band - clears the warm-up
    # guard by 2 W.  What separates them is the end: this rider returns to ride
    # pace, where a ramp tester's power falls off a cliff.
    stream = _synth_minutes(
        [(140.0, 14.0)] * 12
        + [(150.0 + 12.0 * i, 10.0 + 0.4 * i) for i in range(1, 9)]
        + [(150.0, 14.0)] * 20
    )
    assert power.ramp_test_ftp_candidate(stream) == 0.0


def test_ramp_candidate_rejects_a_ramp_shaped_workout_with_no_warm_up():
    # Held by the collapse rule.  In a 20 W/min ramp every step is itself 20 W
    # below the next, so each one satisfies the "clearly lower warm-up minute"
    # for the step after it and the warm-up guard has almost no force.  This
    # stream has no warm-up at all and still has to be rejected.
    stream = _synth_minutes(
        [(196.0, 6.0)] * 3
        + [(200.0 + 20.0 * i, 7.0) for i in range(8)]
        + [(200.0, 8.0)] * 10
    )
    assert power.ramp_test_ftp_candidate(stream) == 0.0


def test_ramp_candidate_rejects_a_pyramid():
    # Held by the collapse rule, and the sharpest false positive of the lot:
    # the ride is under 20 minutes, so best_20min_power is 0.0 and the whole
    # FTP estimate would come from misreading this as a ramp.
    stream = _synth_minutes(
        [(120.0, 6.0)] * 2
        + [(200.0 + 20.0 * i, 8.0) for i in range(8)]
        + [(340.0 - 20.0 * i, 8.0) for i in range(1, 8)]
    )
    assert power.best_20min_power(stream) == 0.0
    assert power.ramp_test_ftp_candidate(stream) == 0.0
    assert power.estimate_ftp([stream]) == 0.0


def test_ramp_candidate_rejects_a_rising_interval_set():
    # Held by the slope band: three-minute intervals at rising intensity with
    # recoveries between them climb overall but never minute on minute.
    stream = _synth_minutes(
        [(110.0, 6.0)] * 3
        + [(200.0, 12.0)] * 3 + [(110.0, 20.0)] * 2
        + [(240.0, 14.0)] * 3 + [(110.0, 20.0)] * 2
        + [(280.0, 15.0)] * 3 + [(110.0, 20.0)] * 2
        + [(90.0, 8.0)] * 5
    )
    assert power.ramp_test_ftp_candidate(stream) == 0.0


def test_ramp_candidate_rejects_a_sprint_in_a_steady_ride():
    # Held by the slope band.
    stream = _synth_minutes([(180.0, 15.0)] * 30)
    sprint_at = 12 * 60
    for i in range(sprint_at, sprint_at + 20):
        stream[i] = 700.0
    assert power.ramp_test_ftp_candidate(stream) == 0.0


def test_ramp_candidate_rejects_a_noisy_steady_ride():
    # Held by the slope band: noise on its own must never build a ramp.
    stream = _synth_minutes([(210.0, 14.0)] * 35)
    assert power.ramp_test_ftp_candidate(stream) == 0.0
