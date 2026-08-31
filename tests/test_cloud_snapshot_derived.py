"""Derived local snapshot objects for the optional cloud sync plane."""

import datetime as dt
import json

from wattracker import db
from wattracker.cloud.models import SyncBatch
from wattracker.cloud.snapshot import DETAIL_MAX_POINTS, snapshot_batch, snapshot_objects


def _activity(path, user_id, number, start_time, seconds=3000):
    power = [180.0 + (index % 40) for index in range(seconds)]
    heartrate = [135.0 + (index % 8) for index in range(seconds)]
    return db.insert_activity(
        user_id,
        {
            "dedup_hash": f"derived-{number}",
            "filename": f"ride-{number}.fit",
            "start_time": start_time,
            "duration_s": seconds,
            "distance_m": 25_000.0,
            "avg_power": 190.0,
            "avg_hr": 138.0,
            "np": 195.0,
            "if_": 0.78,
            "tss": 65.0,
            "streams": {
                "time": list(range(seconds)),
                "power": power,
                "heartrate": heartrate,
                "cadence": [88.0] * seconds,
                "altitude": [100.0 + (index % 10) for index in range(seconds)],
            },
        },
        path=str(path),
    )


def _fixture_db(tmp_path):
    path = tmp_path / "derived.db"
    db.init_db(str(path))
    user_id = db.create_user("rider", "not-a-password", path=str(path))
    db.save_user_settings(
        user_id,
        {"ftp": 240, "hr_max": 190, "weight_kg": 70, "timezone": "UTC"},
        path=str(path),
    )
    db.save_rider_profile(
        user_id,
        {"ftp": 235, "cp": 250, "wprime": 20_000, "hr_max": 188},
        path=str(path),
    )
    db.add_ftp_entry(user_id, "2026-08-20", 230, path=str(path))
    db.add_ftp_entry(user_id, "2026-08-21", 240, path=str(path))
    db.record_weight(user_id, "2026-08-20", 69.5, path=str(path))
    _activity(path, user_id, 1, "2026-08-20T10:00:00")
    _activity(path, user_id, 2, "2026-08-21T10:00:00")
    plan_id = db.create_plan(
        user_id, "Base", "2026-08-20", 2,
        recipe={"goal": "ftp"}, path=str(path),
    )
    db.add_plan_workout(
        plan_id, user_id, "2026-08-20", "Endurance", "endurance", 3600, 50,
        "{}", path=str(path),
    )
    db.add_standalone_workout(
        user_id, "one-off", "2026-08-21", "Recovery", "recovery", 1800, 20,
        "{}", 200, path=str(path),
    )
    db.add_race_date(
        user_id, "2026-08-22", "A", "Test race", 60, path=str(path),
    )
    db.add_ooto_range(
        user_id, "2026-08-23", "2026-08-24", "Travel", path=str(path),
    )
    return path, user_id


def test_derived_snapshot_round_trips_all_kinds_and_keeps_streams_opt_in(tmp_path):
    path, user_id = _fixture_db(tmp_path)

    without_streams = snapshot_objects(path, user_id)
    assert "stream" not in {obj.kind for obj in without_streams}
    assert any(obj.kind == "activity_detail" for obj in without_streams)
    assert all(
        not {"t", "power", "heartrate", "cadence", "altitude"} & obj.data.keys()
        for obj in without_streams
        if obj.kind == "activity_detail"
    )

    objects = snapshot_objects(path, user_id, include_streams=True)
    kinds = {obj.kind for obj in objects}
    assert {
        "profile", "training_state", "ftp_history", "load_point", "curve",
        "volume_week", "calendar_day", "activity", "activity_detail", "stream",
    } <= kinds
    assert all(
        len(values) <= DETAIL_MAX_POINTS
        for obj in objects
        if obj.kind == "stream"
        for values in obj.data["streams"].values()
        if isinstance(values, list)
    )
    assert all(
        "user_id" not in json.dumps(obj.data, sort_keys=True)
        and "path" not in json.dumps(obj.data, sort_keys=True)
        for obj in objects
    )

    wire = {
        "batch_id": "derived-fixture",
        "revision": 1,
        "objects": [obj.wire() for obj in objects],
    }
    round_tripped = SyncBatch.from_wire(wire)
    assert [obj.object_id for obj in round_tripped.objects] == [
        obj.object_id for obj in objects
    ]


def test_snapshot_pages_flattened_derived_objects_without_duplicates(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    all_objects = snapshot_objects(path, user_id, include_streams=True)
    pages = []
    offset = 0
    while True:
        page = snapshot_objects(
            path, user_id, include_streams=True, limit=3, offset=offset,
        )
        if not page:
            break
        assert len(page) <= 3
        pages.extend(page)
        offset += len(page)

    assert [obj.object_id for obj in pages] == [
        obj.object_id for obj in all_objects
    ]
    assert len({obj.object_id for obj in pages}) == len(pages)
    assert snapshot_batch(
        path, user_id, batch_id="empty", revision=1, limit=3,
        offset=len(all_objects),
    ) is None


def test_snapshot_publishes_newest_activity_first(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    objects = snapshot_objects(path, user_id, include_streams=True)
    assert objects[0].kind == "activity"
    activities = [
        obj for obj in objects
        if obj.kind == "activity"
    ]
    assert [obj.object_id for obj in activities] == ["activity-2", "activity-1"]


def test_stream_objects_publish_effective_corrected_samples(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    activity_id = _activity(path, user_id, 3, "2026-08-22T10:00:00", seconds=10)
    conn = db.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO power_sample_corrections "
            "(user_id, activity_id, start_index, end_index, ftp_basis, created) "
            "VALUES (?, ?, 3, 5, 240, ?)",
            (user_id, activity_id, dt.datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    stream = next(
        obj for obj in snapshot_objects(path, user_id, include_streams=True)
        if obj.object_id == f"stream-{activity_id}"
    )
    assert stream.data["streams"]["power"][3:6] == [None, None, None]


def test_activity_detail_uses_stored_date_for_ftp_history(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    db.save_user_settings(
        user_id, {"timezone": "Asia/Tokyo"}, path=str(path),
    )
    db.add_ftp_entry(user_id, "2026-01-01", 200, path=str(path))
    db.add_ftp_entry(user_id, "2026-01-02", 300, path=str(path))
    activity_id = _activity(
        path, user_id, 4, "2026-01-01T23:30:00", seconds=10,
    )

    detail = next(
        obj for obj in snapshot_objects(path, user_id)
        if obj.object_id == f"activity-detail-{activity_id}"
    )
    assert detail.data["zones"]["power"]["anchor"] == 200.0
    assert detail.data["zones"]["power"]["source"] == (
        "Training FTP as of 2026-01-01"
    )
