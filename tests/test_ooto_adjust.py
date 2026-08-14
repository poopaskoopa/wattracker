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
