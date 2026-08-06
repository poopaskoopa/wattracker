"""Regression tests for the import-side FTP rescore."""
import datetime as dt

import pytest

from wattracker import db
from wattracker.ingest import importer


def _parsed_ride(start_time="2026-08-05T10:00:00", watts=263.0, seconds=3600):
    return {
        "start_time": start_time,
        "duration_s": seconds,
        "streams": {
            "time": [None] * seconds,
            "power": [watts] * seconds,
            "heartrate": [140.0] * seconds,
            "cadence": [90.0] * seconds,
            "distance": list(range(seconds)),
            "altitude": [0.0] * seconds,
        },
    }


def test_scan_rescores_new_imports_after_ftp_evaluation(user_id, tmp_path, monkeypatch):
    activity_dir = tmp_path / "Activities"
    activity_dir.mkdir()
    (activity_dir / "ride.fit").write_bytes(b"fit")
    frozen = dt.datetime(2026, 8, 5, 12, 0)

    monkeypatch.setattr(importer, "utc_now", lambda: frozen)
    monkeypatch.setattr(importer, "parse_fit", lambda path: _parsed_ride())

    result = importer.scan_activities(user_id, directory=str(activity_dir))

    assert result["imported"] == 1
    activity = db.list_activities(user_id)[0]
    assert activity["if_"] == pytest.approx(1.0, abs=0.001)
    assert activity["tss"] == pytest.approx(100.0, abs=0.2)


def test_rescore_uses_ftp_effective_on_each_activity_date(user_id):
    db.add_ftp_entry(user_id, "2026-07-01", 200.0, "manual")
    db.add_ftp_entry(user_id, "2026-08-01", 260.0, "manual")
    old_id = db.insert_activity(
        user_id,
        {
            "dedup_hash": "old-ftp-basis",
            "filename": "old.fit",
            "start_time": "2026-07-15T10:00:00",
            "duration_s": 3600,
            "np": 260.0,
            "if_": 1.3,
            "tss": 169.0,
        },
    )
    new_id = db.insert_activity(
        user_id,
        {
            "dedup_hash": "new-ftp-basis",
            "filename": "new.fit",
            "start_time": "2026-08-15T10:00:00",
            "duration_s": 3600,
            "np": 260.0,
            "if_": 1.0,
            "tss": 100.0,
        },
    )

    assert importer.rescore_imported_activities(user_id, [new_id, old_id]) == 2

    rows = {row["id"]: row for row in db.list_activities(user_id)}
    assert rows[old_id]["if_"] == pytest.approx(1.3, abs=0.001)
    assert rows[old_id]["tss"] == pytest.approx(169.0, abs=0.1)
    assert rows[new_id]["if_"] == pytest.approx(1.0, abs=0.001)
    assert rows[new_id]["tss"] == pytest.approx(100.0, abs=0.1)


def test_rescore_skips_activities_without_valid_dates(user_id):
    db.add_ftp_entry(user_id, "2026-08-01", 260.0, "manual")
    invalid_ids = []
    for i, start_time in enumerate(
        (None, "", "not-a-date", "2026-08-01garbage", "2026-08-01Tbad")
    ):
        invalid_ids.append(
            db.insert_activity(
                user_id,
                {
                    "dedup_hash": f"invalid-date-{i}",
                    "filename": f"invalid-{i}.fit",
                    "start_time": start_time,
                    "duration_s": 3600,
                    "np": 260.0,
                    "if_": 1.315,
                    "tss": 172.9,
                },
            )
        )

    assert importer.rescore_imported_activities(user_id, invalid_ids) == 0
    rows = {row["id"]: row for row in db.list_activities(user_id)}
    for activity_id in invalid_ids:
        assert rows[activity_id]["if_"] == pytest.approx(1.315)
        assert rows[activity_id]["tss"] == pytest.approx(172.9)


def test_rescore_processes_imports_in_bounded_batches(user_id, monkeypatch):
    db.add_ftp_entry(user_id, "2026-08-01", 263.0, "manual")
    conn = db.connect()
    try:
        conn.executemany(
            """
            INSERT INTO activities
              (user_id, dedup_hash, filename, start_time, duration_s, np, if_, tss)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    user_id,
                    f"batch-{i}",
                    f"ride-{i}.fit",
                    "2026-08-02T10:00:00",
                    3600,
                    263.0,
                    1.315,
                    172.9,
                )
                for i in range(1001)
            ],
        )
        conn.commit()
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM activities WHERE user_id = ? ORDER BY id",
                (user_id,),
            )
        ]
    finally:
        conn.close()

    batch_sizes = []

    def record_update(_user_id, summaries, path=None):
        batch_sizes.append(len(summaries))
        return len(summaries)

    monkeypatch.setattr(db, "update_activity_ftp_metrics", record_update)

    assert importer.rescore_imported_activities(user_id, ids) == 1001
    assert batch_sizes == [500, 500, 1]
