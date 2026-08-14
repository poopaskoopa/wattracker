from wattracker.prescribe.ooto_adjust import evaluate_ooto


def row(i, date, kind="endurance", **extra):
    return {"id": i, "date": date, "type": kind, "duration_s": 3600,
            "origin": "generated", "completed_activity_id": None,
            "adapted": None, **extra}


def result(workouts, **kwargs):
    return evaluate_ooto({}, workouts, "2026-08-10", "2026-08-12",
                         "2026-08-01", **kwargs)


def kinds(plan, name):
    return next(o for o in plan["options"] if o["kind"] == name)


def test_inclusive_boundaries_and_hard_selection():
    out = result([row(1, "2026-08-10", "threshold"),
                  row(2, "2026-08-12", "vo2max"), row(3, "2026-08-20")])
    assert [x["date"] for x in out["affected"]] == ["2026-08-10", "2026-08-12"]
    assert all(x["key"] for x in out["affected"])
    assert kinds(out, "reschedule")["actions"][0]["target_date"] == "2026-08-20"


def test_noop_exclusions_and_no_candidates():
    out = result([row(1, "2026-08-10", "threshold", completed_activity_id=9),
                  row(2, "2026-08-12", "threshold", adapted="recovery"),
                  row(3, "2026-08-11", "threshold"), row(4, "2026-08-20")],
                 race_dates=["2026-08-20"])
    assert [item["id"] for item in out["affected"]] == [3]
    assert out["recommended_option"] == "skip"
    assert all("actions" in option for option in out["options"])


def test_manual_past_ooto_and_race_rows_are_excluded():
    out = result([row(1, "2026-08-09", "threshold"),
                  row(2, "2026-08-10", "threshold", origin="manual"),
                  row(3, "2026-08-13", "endurance"),
                  row(4, "2026-08-20", "endurance")], race_dates=["2026-08-13"])
    assert out["affected"] == []
    assert kinds(out, "reschedule")["actions"] == []


def test_existing_adjustment_rows_are_not_reproposed():
    out = result([
        row(1, "2026-08-10", "threshold", adjustment_state="ooto_canceled"),
        row(2, "2026-08-20"),
    ])
    assert out["affected"] == []
    assert all(option["actions"] == [] for option in out["options"])


def test_recommendation_and_fingerprints_are_deterministic():
    workouts = [row(2, "2026-08-20"), row(1, "2026-08-10", "threshold")]
    a, b = result(workouts), result(list(reversed(workouts)))
    assert a == b
    action = kinds(a, "reschedule")["actions"][0]
    assert len(action["expected_source_fingerprint"]) == 64
    assert len(action["expected_target_fingerprint"]) == 64
    assert a["recommended_option"] == "reschedule"


# -------------------------------------------------------- blocker regressions

def test_rescheduled_key_workouts_are_never_placed_on_adjacent_days():
    """Two key workouts cancelled by one range must not end up back-to-back.

    A target chosen DURING the loop has to join the adjacency set; without that
    the second key workout only sees the hard days that existed before the
    proposal started and happily takes the day next to the one just claimed.
    """
    out = result([row(1, "2026-08-10", "threshold"),
                  row(2, "2026-08-12", "vo2max"),
                  row(3, "2026-08-15"), row(4, "2026-08-16")])
    reschedule = kinds(out, "reschedule")
    assert [a["target_date"] for a in reschedule["actions"]] == ["2026-08-15"]
    assert reschedule["affected_keys"] == 2
    assert reschedule["resolved_keys"] == 1
    assert reschedule["unresolved"] == [
        {"id": 2, "date": "2026-08-12", "type": "vo2max"},
    ]


def test_unequal_durations_move_and_report_the_volume_they_cost():
    out = result([row(1, "2026-08-10", "threshold", duration_s=3000),
                  row(2, "2026-08-20", duration_s=7800)])
    reschedule = kinds(out, "reschedule")
    assert [a["target_date"] for a in reschedule["actions"]] == ["2026-08-20"]
    assert reschedule["resolved_keys"] == 1
    # The moved session keeps its own 3000 s prescription and the 7800 s easy
    # ride it lands on is lost, so the plan is 4800 s lighter.
    assert reschedule["volume_delta_s"] == -4800


def test_a_key_workout_longer_than_the_slot_is_refused_not_grown():
    """Weekly minutes must never grow, so a longer source is left in place."""
    out = result([row(1, "2026-08-10", "threshold", duration_s=9000),
                  row(2, "2026-08-20", duration_s=7800)])
    reschedule = kinds(out, "reschedule")
    assert reschedule["actions"] == []
    assert reschedule["volume_delta_s"] == 0
    assert [item["id"] for item in reschedule["unresolved"]] == [1]


def test_rebalance_relocates_nothing_and_holds_every_duration():
    out = result([row(1, "2026-08-10", "threshold", duration_s=3000, tss=50.9),
                  row(2, "2026-08-17", duration_s=7800, tss=101.0),
                  row(3, "2026-08-24", duration_s=4020, tss=49.6)])
    rebalance = kinds(out, "rebalance")
    assert [a["target_date"] for a in rebalance["actions"]] == [
        "2026-08-17", "2026-08-24",
    ]
    assert all(a["mode"] == "rebalance" for a in rebalance["actions"])
    # Same length, one rung up, never a hard session.
    assert [a["duration_s"] for a in rebalance["actions"]] == [7800, 4020]
    assert {a["new_type"] for a in rebalance["actions"]} == {"tempo"}
    assert rebalance["volume_delta_s"] == 0
    assert rebalance["resolved_keys"] == 1
    # Nothing is relocated: every action names the same canceled source.
    assert {a["source_date"] for a in rebalance["actions"]} == {"2026-08-10"}


def test_the_two_options_are_not_the_same_edit():
    workouts = [row(1, "2026-08-10", "threshold", duration_s=3000, tss=50.9),
                row(2, "2026-08-17", duration_s=7800, tss=101.0)]
    out = result(workouts)
    reschedule, rebalance = kinds(out, "reschedule"), kinds(out, "rebalance")
    assert reschedule["actions"] != rebalance["actions"]
    assert reschedule["volume_delta_s"] == -4800
    assert rebalance["volume_delta_s"] == 0
