"""Tests for the measured rider-capacity layer (HRmax, W'/kg, power ratios)."""
import datetime as dt

import pytest

from wattracker import db
from wattracker.analysis.state import TrainingState
from wattracker.metrics import rider


def _hr_ride(base=165.0, peak=None, peak_len=0, seconds=1800):
    """A HR stream that sits at `base` with an optional `peak` block in it."""
    stream = [base] * seconds
    if peak is not None and peak_len > 0:
        mid = seconds // 2
        for i in range(mid, mid + peak_len):
            stream[i] = peak
    return stream


def _insert(user_id, idx, streams, start=None):
    rec = {
        "dedup_hash": f"hr-{user_id}-{idx}",
        "filename": f"ride{idx}.fit",
        "start_time": start or f"2026-06-{idx + 1:02d}T10:00:00",
        "duration_s": len(streams.get("heartrate") or []) or 60,
        "distance_m": 1000.0, "avg_power": 200.0, "avg_hr": 150.0,
        "np": 205.0, "if_": 0.8, "tss": 50.0,
        "streams": streams,
    }
    return db.insert_activity(user_id, rec)


# ------------------------------------------------------- sustained HR peak
def test_short_spike_is_not_reported_as_hr_max():
    # 150-170 bpm ride with a 3-sample 240 bpm sensor artefact. A raw max()
    # would report 240; the rolling mean must dilute it away.
    stream = [150.0 + (i % 21) for i in range(1800)]
    for i in range(900, 903):
        stream[i] = 240.0
    peak = rider.sustained_hr_peak(stream)
    assert peak is not None
    assert peak < 190.0
    assert max(stream) == 240.0  # the spike really is in the data


def test_three_sample_artefact_on_a_flat_ride_is_well_contained():
    # Defect-1 regression: a genuinely 155-160 bpm ride carrying a 3-sample
    # ~240 artefact reported 183 with the old 10s window. The 30s window keeps
    # it near 166, so require well under the old figure.
    stream = [155.0 + (i % 6) for i in range(1800)]
    stream[900:903] = [240.0, 242.0, 238.0]
    peak = rider.sustained_hr_peak(stream)
    assert peak is not None
    assert peak < 172.0


def test_genuine_sustained_effort_is_reported():
    # A 60s plateau at 185 is physiological, not an artefact.
    stream = _hr_ride(base=160.0, peak=185.0, peak_len=60)
    assert rider.sustained_hr_peak(stream) == pytest.approx(185.0, abs=0.5)


def test_genuine_45s_plateau_is_undiminished():
    # Defect-1 regression the other way: the window must not be so long that a
    # real 45s maximal finish gets blunted.
    stream = _hr_ride(base=160.0, peak=186.0, peak_len=45)
    assert rider.sustained_hr_peak(stream) == pytest.approx(186.0, abs=0.5)


def test_sustained_peak_survives_empty_and_junk_streams():
    assert rider.sustained_hr_peak([]) is None
    assert rider.sustained_hr_peak([None] * 100) is None
    assert rider.sustained_hr_peak(["x", None, "", {}]) is None
    # Junk mixed with enough real samples still yields the real peak.
    stream = ["bad", None] * 5 + [180.0] * 40
    assert rider.sustained_hr_peak(stream) == pytest.approx(180.0)


def test_stream_shorter_than_window_is_none():
    assert rider.sustained_hr_peak([180.0] * (rider.HR_SMOOTH_WINDOW_S - 1)) is None


# ------------------------------------------------------- detect_hr_max
def test_detect_hr_max_across_activities():
    streams = [_hr_ride(peak=185.0, peak_len=60) for _ in range(10)]
    hr, n = rider.detect_hr_max(streams)
    assert n == 10
    assert hr == pytest.approx(185.0, abs=0.5)


def test_one_corrupt_activity_does_not_move_the_answer():
    good = [_hr_ride(peak=185.0, peak_len=60) for _ in range(9)]
    # A whole bad file: 20 minutes of in-bounds-but-wrong 225 bpm cross-talk,
    # which smoothing cannot remove - the cross-activity percentile must.
    corrupt = _hr_ride(base=225.0)
    hr, n = rider.detect_hr_max(good + [corrupt])
    assert n == 10
    assert hr == pytest.approx(185.0, abs=0.5)


@pytest.mark.parametrize("n,k", [(10, 2), (20, 2), (30, 3), (50, 4), (100, 6)])
def test_detection_does_not_degrade_with_more_history(n, k):
    # Defect-2 regression. A true HRmax of 188 reached on k of n rides, the
    # rest scattered 168-183. The old 90th-percentile rule discarded a fixed
    # FRACTION of the top peaks, so it read 5-7 bpm low for every n above ~10 -
    # more history made the answer worse. Corroboration must be n-independent.
    streams = [_hr_ride(peak=188.0, peak_len=60) for _ in range(k)]
    for i in range(n - k):
        streams.append(_hr_ride(peak=168.0 + (i % 16), peak_len=60))
    hr, count = rider.detect_hr_max(streams)
    assert count == n
    assert hr == pytest.approx(188.0, abs=0.5)


def test_single_corrupt_file_among_twenty_good_rides():
    # 20 real rides topping out at 180, plus one file stuck at a sustained
    # 215 bpm. 215 is in bounds and survives smoothing, so only the
    # "no other activity corroborates it" rule can reject it.
    streams = [_hr_ride(peak=180.0, peak_len=60) for _ in range(4)]
    for i in range(16):
        streams.append(_hr_ride(peak=165.0 + (i % 14), peak_len=60))
    streams.append(_hr_ride(base=215.0))
    hr, count = rider.detect_hr_max(streams)
    assert count == 21
    assert hr == pytest.approx(180.0, abs=0.5)


def test_no_corroborated_peak_returns_none():
    # Six activities whose peaks are all more than the tolerance apart: nothing
    # is reproducible, so there is nothing to report.
    streams = [
        _hr_ride(base=140.0, peak=150.0 + 10 * i, peak_len=60) for i in range(6)
    ]
    hr, count = rider.detect_hr_max(streams)
    assert count == 6
    assert hr is None


def test_too_few_activities_returns_none():
    streams = [_hr_ride(peak=185.0, peak_len=60) for _ in range(4)]
    hr, n = rider.detect_hr_max(streams)
    assert hr is None
    assert n == 4


def test_out_of_bounds_streams_are_rejected():
    # All-300 (unit confusion / cross-talk) and all-zero (dropout) streams.
    hr, n = rider.detect_hr_max([[300.0] * 600] * 6 + [[0.0] * 600] * 6)
    assert hr is None
    assert n == 0


# ------------------------------------------------------- normalized capacity
def test_wprime_per_kg():
    assert rider.wprime_per_kg(21000.0, 70.0) == pytest.approx(300.0)
    assert rider.cp_per_kg(280.0, 70.0) == pytest.approx(4.0)


@pytest.mark.parametrize("wprime,weight", [
    (None, 70.0), (21000.0, None), (21000.0, 0.0), (21000.0, -5.0), (None, None),
])
def test_wprime_per_kg_none_when_inputs_missing(wprime, weight):
    assert rider.wprime_per_kg(wprime, weight) is None
    assert rider.cp_per_kg(wprime, weight) is None


# ------------------------------------------------------- power ratios
def test_sprint_and_vo2_ratio():
    mmp = {5: 1000.0, 60: 500.0, 300: 350.0}
    assert rider.power_ratio(mmp, 5, 250.0) == pytest.approx(4.0)
    assert rider.power_ratio(mmp, 300, 250.0) == pytest.approx(1.4)


@pytest.mark.parametrize("mmp,ftp", [
    ({5: 1000.0}, 0.0), ({5: 1000.0}, None), ({60: 500.0}, 250.0),
    ({}, 250.0), (None, 250.0), ({5: 0.0}, 250.0),
])
def test_sprint_ratio_none_safe(mmp, ftp):
    assert rider.power_ratio(mmp, 5, ftp) is None


# ------------------------------------------------------- assembly
def test_for_user_with_no_data_is_all_none(user_id):
    m = rider.for_user(user_id, state=TrainingState(ftp=0.0))
    assert m.ftp is None
    assert m.weight_kg is None
    assert m.hr_max is None
    assert m.hr_max_source is None
    assert m.n_hr_activities == 0
    assert m.sprint_ratio is None
    assert m.wprime_j_per_kg is None
    assert m.peak_5s is None
    assert m.to_dict()["sprint_ratio"] is None


def test_for_user_builds_state_when_not_supplied(user_id):
    # No rides at all: must not raise even though it has to build a state.
    m = rider.for_user(user_id)
    assert isinstance(m, rider.RiderMetrics)
    assert m.hr_max is None


def test_for_user_full(user_id):
    db.save_user_settings(user_id, {"weight_kg": 70.0})
    for i in range(10):
        _insert(user_id, i, {"heartrate": _hr_ride(peak=185.0, peak_len=60)})
    state = TrainingState(
        ftp=250.0, cp=280.0, wprime=21000.0,
        mmp={5: 1000.0, 60: 500.0, 300: 350.0},
    )
    m = rider.for_user(user_id, state=state, now=dt.datetime(2026, 6, 20))
    assert m.ftp == pytest.approx(250.0)
    assert m.weight_kg == pytest.approx(70.0)
    assert m.hr_max == pytest.approx(185.0, abs=0.5)
    assert m.hr_max_source == "measured"
    assert m.n_hr_activities == 10
    assert m.wprime_j_per_kg == pytest.approx(300.0)
    assert m.cp_w_per_kg == pytest.approx(4.0)
    assert m.peak_5s == pytest.approx(1000.0)
    assert m.peak_60s == pytest.approx(500.0)
    assert m.peak_300s == pytest.approx(350.0)
    assert m.sprint_ratio == pytest.approx(4.0)
    assert m.vo2_ratio == pytest.approx(1.4)


def test_manual_hr_max_overrides_detection(user_id):
    for i in range(10):
        _insert(user_id, i, {"heartrate": _hr_ride(peak=185.0, peak_len=60)})
    db.set_user_hr_max(user_id, 196)
    m = rider.for_user(user_id, state=TrainingState(ftp=250.0),
                       now=dt.datetime(2026, 6, 20))
    assert m.hr_max == pytest.approx(196.0)
    assert m.hr_max_source == "manual"


def test_for_user_survives_corrupt_streams(user_id):
    _insert(user_id, 0, {})
    _insert(user_id, 1, {"heartrate": []})
    _insert(user_id, 2, {"heartrate": [None] * 600})
    _insert(user_id, 3, {"heartrate": ["junk", None, 3.0]})
    _insert(user_id, 4, {"heartrate": [0.0] * 600})
    m = rider.for_user(user_id, state=TrainingState(ftp=250.0),
                       now=dt.datetime(2026, 6, 20))
    assert m.hr_max is None
    assert m.hr_max_source is None
    assert m.ftp == pytest.approx(250.0)
