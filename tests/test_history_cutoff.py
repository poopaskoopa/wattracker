from wattracker import db
from wattracker.cloud.snapshot import snapshot_batch, snapshot_convergence, snapshot_objects
from wattracker.cloud.models import CloudObject
from wattracker.metrics import curve_store


def _activity(path, uid, start, ident, power=None):
    power = power or [200, 200]
    return db.insert_activity(uid, {
        "dedup_hash": ident, "filename": ident, "start_time": start,
        "duration_s": 3600, "distance_m": 1000, "avg_power": 200,
        "avg_hr": 130, "np": 200, "if_": .8, "tss": 64,
        "streams": {"power": power},
    }, path)


def test_cutoff_is_inclusive_and_reversible_across_aggregates(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    _activity(path, uid, "2026-01-01T23:30:00", "old")
    boundary = _activity(path, uid, "2026-01-02T05:30:00", "boundary")
    db.save_user_settings(uid, {"timezone": "America/New_York",
                                "history_start_date": "2026-01-02"}, path)
    assert [a["id"] for a in db.list_activities(uid, path)] == [boundary]
    assert db.get_activity(uid, 1, path) is None
    assert db.daily_tss(uid, path)
    assert db.weekly_volume(uid, path)[0]["tss"] == 64.0
    db.save_user_settings(uid, {"history_start_date": None}, path)
    assert len(db.list_activities(uid, path)) == 2


def test_cutoff_calendar_month_uses_rider_local_date(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    activity_id = _activity(path, uid, "2026-01-01T02:00:00", "local-december")
    db.save_user_settings(uid, {
        "timezone": "America/New_York",
        "history_start_date": "2025-12-31",
    }, path)
    assert [a["id"] for a in db.activities_for_month_unlinked(
        uid, 2025, 12, path
    )] == [activity_id]
    assert db.activities_for_month_unlinked(uid, 2026, 1, path) == []


def test_cutoff_changes_analysis_fingerprint_and_curve(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    _activity(path, uid, "2026-01-01T12:00:00", "old")
    assert db.activity_is_visible(uid, "2026-01-01T12:00:00", path)
    db.save_user_settings(uid, {"history_start_date": "2026-01-02"}, path)
    assert not db.activity_is_visible(uid, "2026-01-01T12:00:00", path)


def test_current_ftp_and_persisted_curve_follow_cutoff_reversibly(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    old = _activity(path, uid, "2026-01-01T12:00:00", "old", [300] * 1200)
    new = _activity(path, uid, "2026-02-01T12:00:00", "new", [100] * 1200)
    db.add_ftp_entry(uid, "2026-01-01", 180, path=path)
    db.add_ftp_entry(uid, "2026-02-01", 220, path=path)
    assert db.latest_ftp(uid, path)["ftp_watts"] == 220
    db.save_user_settings(uid, {"history_start_date": "2026-03-01"}, path)
    assert db.latest_ftp(uid, path) is None
    db.save_user_settings(uid, {"history_start_date": "2026-02-01"}, path)
    assert db.latest_ftp(uid, path)["ftp_watts"] == 220
    assert old != new
    db.save_user_settings(uid, {"history_start_date": None}, path)
    assert db.latest_ftp(uid, path)["ftp_watts"] == 220

    # The persisted curve is dirty on each setting transition and therefore
    # removes/restores the historical peak rather than serving stale cache data.
    before = curve_store.ensure(uid, path)
    assert max(before.values()) == 300
    db.save_user_settings(uid, {"history_start_date": "2026-02-01"}, path)
    after = curve_store.all_time(uid, path)
    assert max(after.values()) == 100
    db.save_user_settings(uid, {"history_start_date": None}, path)
    assert max(curve_store.all_time(uid, path).values()) == 300


def test_hidden_activity_corrections_are_not_readable(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    activity_id = _activity(path, uid, "2026-01-01T12:00:00", "old")
    assert db.apply_power_correction(
        uid, activity_id, 0, 0, 200, "test",
        {"avg_power": 200, "np": 200, "if_": .8, "tss": 64}, path=path
    )
    db.save_user_settings(uid, {"history_start_date": "2026-02-01"}, path)
    assert db.list_power_corrections(uid, path=path) == []
    assert db.power_correction_activity(uid, activity_id, path) is None


def test_recent_and_pagination_skip_hidden_rows_without_invalid_date_crash(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    for n in range(3):
        _activity(path, uid, f"2026-01-0{n + 1}T12:00:00", f"old-{n}")
    visible = _activity(path, uid, "2026-08-29T12:00:00", "visible")
    invalid = _activity(path, uid, None, "invalid")
    db.save_user_settings(uid, {"history_start_date": "2026-08-01"}, path)
    assert [a["id"] for a in db.recent_full_activities(uid, 365, path)] == [visible]
    assert db.activity_ids_after(uid, 0, 1, path) == [visible]
    db.save_user_settings(uid, {"history_start_date": None}, path)
    assert invalid not in db.daily_tss(uid, path)


def test_changing_cutoff_invalidates_scanned_file_cache(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    db.record_scanned_file(uid, "/rides/old.fit", 1.0, 2, path)
    db.save_user_settings(uid, {"history_start_date": "2026-08-01"}, path)
    assert db.seen_files(uid, path) == {}
    db.record_scanned_file(uid, "/rides/old.fit", 1.0, 2, path)
    db.save_user_settings(uid, {"history_start_date": None}, path)
    assert db.seen_files(uid, path) == {}


def test_v34_to_v35_migration_adds_nullable_cutoff_in_place(tmp_path, monkeypatch):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    db.save_user_settings(uid, {"timezone": "UTC"}, path)
    conn = db.connect(path)
    conn.execute("ALTER TABLE user_settings RENAME TO user_settings_new")
    conn.execute("""CREATE TABLE user_settings (
        user_id INTEGER PRIMARY KEY, ftp REAL, zwift_id TEXT,
        activities_dir TEXT, workouts_dir TEXT, zwift_email TEXT,
        zwift_password_enc TEXT, weight_kg REAL, hr_max INTEGER,
        timezone TEXT, FOREIGN KEY(user_id) REFERENCES users(id))""")
    conn.execute("""INSERT INTO user_settings
        (user_id, ftp, zwift_id, activities_dir, workouts_dir, zwift_email,
         zwift_password_enc, weight_kg, hr_max, timezone)
        SELECT user_id, ftp, zwift_id, activities_dir, workouts_dir, zwift_email,
               zwift_password_enc, weight_kg, hr_max, timezone
        FROM user_settings_new""")
    conn.execute("DROP TABLE user_settings_new")
    conn.execute("PRAGMA user_version = 34")
    conn.commit()
    conn.close()
    monkeypatch.setattr("wattracker.backup.create_backup", lambda *a, **k: None)
    db.init_db(path)
    check = db.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 35
    check.close()
    assert db.get_user_settings(uid, path)["history_start_date"] is None


def test_cloud_snapshot_filters_and_explicitly_converges_with_tombstones(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    _activity(path, uid, "2026-01-01T12:00:00", "old")
    kept = _activity(path, uid, "2026-02-01T12:00:00", "kept")
    db.save_user_settings(uid, {"history_start_date": "2026-02-01"}, path)
    objects = snapshot_objects(path, uid)
    assert [o.object_id for o in objects] == [f"activity-{kept}"]
    prior = {"activity-1": {"kind": "activity", "revision": 3},
             objects[0].object_id: {"kind": "activity", "revision": 4}}
    converged = snapshot_convergence(prior, objects, complete=True)
    deleted = [o for o in converged if o.deleted]
    assert len(deleted) == 1
    assert deleted[0].object_id == "activity-1"
    assert deleted[0].revision == 4
    batch = snapshot_batch(path, uid, batch_id="batch", revision=1,
                           previously_published=prior)
    assert any(obj.deleted for obj in batch.objects)


def test_cloud_snapshot_does_not_tombstone_a_partial_page_or_raise_on_noop(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    uid = db.create_user("rider", "hash", path)
    first = _activity(path, uid, "2026-02-01T12:00:00", "first")
    second = _activity(path, uid, "2026-02-02T12:00:00", "second")
    current = snapshot_objects(path, uid, limit=1)
    prior = {
        f"activity-{first}": {"kind": "activity", "revision": 1},
        f"activity-{second}": {"kind": "activity", "revision": 2},
    }
    assert not any(
        obj.deleted
        for obj in snapshot_convergence(prior, current)
    )
    db.save_user_settings(uid, {"history_start_date": "2027-01-01"}, path)
    assert snapshot_batch(
        path, uid, batch_id="empty", revision=1,
        previously_published={},
    ) is None
