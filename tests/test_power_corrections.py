"""Full-resolution, immutable, reversible power-sample corrections."""
import datetime as dt
import sqlite3

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import auth, db, power_corrections
from wattracker.analysis import activity_cache, pipeline, power_profile
from wattracker.server import create_app


def _activity(
    user_id,
    power,
    *,
    start="2026-07-01T10:00:00",
    filename="spike.fit",
):
    n = len(power)
    return db.insert_activity(
        user_id,
        {
            "dedup_hash": f"{user_id}-{filename}-{start}",
            "filename": filename,
            "start_time": start,
            "duration_s": n,
            "distance_m": 1234.5,
            "avg_power": sum(power) / n,
            "avg_hr": 145.0,
            "np": 250.0,
            "if_": 1.0,
            "tss": 50.0,
            "streams": {
                "time": [
                    (dt.datetime.fromisoformat(start) + dt.timedelta(seconds=i)).isoformat()
                    for i in range(n)
                ],
                "power": power,
                "heartrate": [140 + i for i in range(n)],
                "cadence": [80 + i for i in range(n)],
                "distance": [float(i) for i in range(n)],
            },
        },
    )


@pytest.fixture(autouse=True)
def _cheap_refresh(monkeypatch):
    monkeypatch.setattr(power_corrections.importer, "evaluate_ftp", lambda uid: False)
    monkeypatch.setattr(power_corrections.profile_store, "refresh", lambda uid: None)
    activity_cache.invalidate()
    yield
    activity_cache.invalidate()


def test_v23_to_v24_preserves_rows_and_creates_audit_table(tmp_path):
    path = str(tmp_path / "v23.db")
    db.init_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO users(username, password_hash, created) VALUES ('kept','h','now')"
    )
    conn.execute("DROP TABLE power_sample_corrections")
    conn.execute("PRAGMA user_version = 23")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 24
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "kept"
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(power_sample_corrections)")
    }
    conn.close()
    assert {
        "user_id", "activity_id", "start_index", "end_index", "ftp_basis",
        "original_avg_power", "original_np", "original_if", "original_tss",
        "undone_at",
    } <= columns


def test_finder_uses_full_resolution_and_shows_exact_neighbors(user_id):
    power = [200.0] * 3000
    power[1499] = 2000.0
    activity_id = _activity(user_id, power)
    detail = pipeline.activity_detail(user_id, activity_id, max_points=10)
    assert max(v for v in detail["power"] if v is not None) < 1000

    found = power_corrections.find_anomalies(user_id, 1000)

    assert len(found) == 1
    candidate = found[0]
    assert (candidate["start_index"], candidate["end_index"]) == (1499, 1499)
    assert candidate["matches"][0]["value"] == 2000.0
    assert candidate["matches"][0]["timestamp"].endswith("24:59")
    assert len(candidate["neighbors"]) == 11
    assert candidate["after"][5]["value"] is None


def test_finder_skips_malformed_and_overflow_streams_without_500(user_id):
    db.insert_activity(user_id, {
        "dedup_hash": "top-level-list",
        "filename": "legacy.fit",
        "start_time": "2026-06-30T10:00:00",
        "duration_s": 1,
        "streams": [1, 2, 3],
    })
    activity_id = db.insert_activity(user_id, {
        "dedup_hash": "numeric-overflow",
        "filename": "overflow.fit",
        "start_time": "2026-07-02T10:00:00",
        "duration_s": 3,
        "streams": {
            "time": [0, 1, 2],
            "power": [100, 10**1000, 2000],
        },
    })

    found = power_corrections.find_anomalies(user_id, 1000)

    assert [(c["activity_id"], c["start_index"]) for c in found] == [
        (activity_id, 2)
    ]
    assert db.recent_power_streams(user_id, days=365) == [
        [100, 10**1000, 2000]
    ]
    power_corrections.apply(user_id, activity_id, 2, 2)
    assert db.get_activity(user_id, activity_id)["streams"]["power"] == [
        100, 10**1000, None,
    ]


def test_finder_bounds_candidate_count_and_long_run_preview(user_id):
    alternating = [2000.0 if i % 2 == 0 else 100.0 for i in range(500)]
    _activity(user_id, alternating, filename="alternating.fit")
    _activity(
        user_id,
        [2000.0] * 5000,
        start="2026-07-02T10:00:00",
        filename="long.fit",
    )

    found = power_corrections.find_anomalies(user_id, 1000)

    assert len(found) == power_corrections.MAX_CANDIDATES
    assert max(len(candidate["neighbors"]) for candidate in found) <= 15
    long_run = found[0]
    assert long_run["sample_count"] == 5000
    assert long_run["preview_truncated"] is True
    assert long_run["applicable"] is False
    assert len(long_run["matches"]) == 5


def test_apply_masks_only_power_preserves_blob_alignment_and_refreshes_summary(user_id):
    activity_id = _activity(user_id, [100.0, 2000.0, 2100.0, 100.0])
    conn = db.connect()
    before_blob = bytes(conn.execute(
        "SELECT streams FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()[0])
    before = db.get_activity(user_id, activity_id)
    conn.close()

    correction_id = power_corrections.apply(user_id, activity_id, 1, 2, "spike")

    after = db.get_activity(user_id, activity_id)
    assert correction_id > 0
    assert after["streams"]["power"] == [100.0, None, None, 100.0]
    for key in ("time", "heartrate", "cadence", "distance"):
        assert after["streams"][key] == before["streams"][key]
        assert len(after["streams"][key]) == len(after["streams"]["power"])
    conn = db.connect()
    row = conn.execute(
        "SELECT streams, avg_power, np, if_, tss, duration_s, distance_m, avg_hr "
        "FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()
    conn.close()
    assert bytes(row["streams"]) == before_blob
    assert row["avg_power"] == pytest.approx(50.0)
    assert row["np"] < 250.0
    assert row["duration_s"] == 4
    assert row["distance_m"] == 1234.5
    assert row["avg_hr"] == 145.0


def test_all_stream_consumers_profile_graph_and_cache_see_mask(user_id):
    power = [200.0] * 1500
    power[0:5] = [2000.0] * 5
    activity_id = _activity(user_id, power)
    digest_before = activity_cache.get_digest(user_id)
    profile_before = power_profile.for_user(user_id)
    peak_before = next(r["all_time"] for r in profile_before["rows"] if r["duration"] == 1)

    power_corrections.apply(user_id, activity_id, 0, 4)

    assert db.full_activities(user_id)[0]["streams"]["power"][:5] == [None] * 5
    assert db.recent_full_activities(user_id, 365)[0]["streams"]["power"][:5] == [None] * 5
    assert db.recent_power_streams(user_id, 365)[0][:5] == [None] * 5
    assert pipeline.activity_detail(user_id, activity_id)["power"][:5] == [None] * 5
    profile_after = power_profile.for_user(user_id)
    peak_after = next(r["all_time"] for r in profile_after["rows"] if r["duration"] == 1)
    assert peak_after < peak_before
    assert activity_cache.get_digest(user_id) is not digest_before


def test_undo_restores_effective_power_and_summary(user_id):
    activity_id = _activity(user_id, [100.0, 2000.0, 100.0])
    original = db.get_activity(user_id, activity_id)
    correction_id = power_corrections.apply(user_id, activity_id, 1, 1)
    assert db.get_activity(user_id, activity_id)["streams"]["power"][1] is None

    power_corrections.undo(user_id, correction_id)

    restored = db.get_activity(user_id, activity_id)
    assert restored["streams"]["power"] == original["streams"]["power"]
    assert restored["avg_power"] == pytest.approx(sum(original["streams"]["power"]) / 3)
    audit = db.list_power_corrections(user_id)
    assert audit[0]["undone_at"] is not None
    assert db.list_power_corrections(user_id, active_only=True) == []


def test_final_undo_restores_inconsistent_summary_and_raw_streams_exactly(user_id):
    activity_id = db.insert_activity(user_id, {
        "dedup_hash": "inconsistent-summary",
        "filename": "inconsistent.fit",
        "start_time": "2026-07-02T11:00:00",
        "duration_s": 3,
        "distance_m": 987.6,
        "avg_power": 100,
        "avg_hr": 141,
        "np": 100,
        "if_": 1,
        "tss": 1,
        "streams": {
            "time": [0, 1, 2],
            "power": [100, 10**1000, 2000],
            "heartrate": [140, 141, 142],
            "cadence": [80, 81, 82],
            "distance": [0, 10, 20],
        },
    })
    before = db.get_activity(user_id, activity_id)
    conn = db.connect()
    before_blob = bytes(conn.execute(
        "SELECT streams FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()["streams"])
    conn.close()

    correction_id = power_corrections.apply(user_id, activity_id, 2, 2)
    power_corrections.undo(user_id, correction_id)

    restored = db.get_activity(user_id, activity_id)
    assert restored["streams"] == before["streams"]
    assert {
        key: restored[key] for key in ("avg_power", "np", "if_", "tss")
    } == {"avg_power": 100, "np": 100, "if_": 1, "tss": 1}
    assert restored["avg_hr"] == 141
    assert restored["distance_m"] == 987.6
    conn = db.connect()
    assert bytes(conn.execute(
        "SELECT streams FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()["streams"]) == before_blob
    conn.close()
    audit = db.list_power_corrections(user_id)[0]
    assert {
        key: audit[key]
        for key in (
            "original_avg_power", "original_np", "original_if", "original_tss"
        )
    } == {
        "original_avg_power": 100,
        "original_np": 100,
        "original_if": 1,
        "original_tss": 1,
    }


def test_multiple_corrections_use_stable_ftp_and_undo_order_has_no_drift(user_id):
    ids = [
        _activity(
            user_id,
            [250.0] * 60,
            start=f"2026-07-0{day}T10:00:00",
            filename=f"order-{day}.fit",
        )
        for day in (3, 4)
    ]
    baseline = power_corrections._summary(
        db.get_activity(user_id, ids[0]), [250.0] * 60, 250.0
    )
    conn = db.connect()
    for activity_id in ids:
        conn.execute(
            "UPDATE activities SET avg_power=?, np=?, if_=?, tss=? WHERE id=?",
            (
                baseline["avg_power"], baseline["np"], baseline["if_"],
                baseline["tss"], activity_id,
            ),
        )
    conn.commit()
    conn.close()

    corrections = []
    for activity_id in ids:
        corrections.append((
            power_corrections.apply(user_id, activity_id, 0, 0),
            power_corrections.apply(user_id, activity_id, 10, 10),
        ))
    audits = db.list_power_corrections(user_id, active_only=True)
    assert {row["ftp_basis"] for row in audits} == {250.0}
    assert {
        (
            row["original_avg_power"], row["original_np"],
            row["original_if"], row["original_tss"],
        )
        for row in audits
    } == {
        (
            baseline["avg_power"], baseline["np"],
            baseline["if_"], baseline["tss"],
        )
    }

    power_corrections.undo(user_id, corrections[0][0])
    power_corrections.undo(user_id, corrections[0][1])
    power_corrections.undo(user_id, corrections[1][1])
    power_corrections.undo(user_id, corrections[1][0])

    for activity_id in ids:
        restored = db.get_activity(user_id, activity_id)
        assert {
            key: restored[key] for key in ("avg_power", "np", "if_", "tss")
        } == baseline


def test_invalid_overlap_large_and_cross_user_requests_change_nothing(user_id):
    other = db.create_user("other", auth.hash_password("password123"))
    activity_id = _activity(user_id, [100.0] * 5000)
    first = power_corrections.apply(user_id, activity_id, 10, 20)
    assert first
    for start, end in ((-1, 2), (20, 25), (4999, 5000), (0, 3600)):
        with pytest.raises(power_corrections.CorrectionError):
            power_corrections.apply(user_id, activity_id, start, end)
    with pytest.raises(power_corrections.CorrectionError):
        power_corrections.apply(other, activity_id, 30, 31)
    with pytest.raises(power_corrections.CorrectionError):
        power_corrections.undo(other, first)
    assert len(db.list_power_corrections(user_id, active_only=True)) == 1
    assert db.list_power_corrections(other) == []


def test_persisted_fingerprint_detects_apply_and_undo_without_explicit_invalidate(user_id):
    activity_id = _activity(user_id, [200.0] * 1500)
    first = activity_cache.get_digest(user_id)
    activity = db.power_correction_activity(user_id, activity_id)
    correction_id = db.apply_power_correction(
        user_id, activity_id, 0, 0, 250.0, None,
        {"avg_power": 199.9, "np": 199.9, "if_": 0.8, "tss": 10.0},
    )
    second = activity_cache.get_digest(user_id)
    assert second is not first
    assert db.undo_power_correction(
        user_id, correction_id,
        {"avg_power": 200.0, "np": 200.0, "if_": 0.8, "tss": 10.0},
    )
    assert activity_cache.get_digest(user_id) is not second


def test_database_trigger_rejects_cross_user_activity_reference(user_id):
    other = db.create_user("trigger-other", auth.hash_password("password123"))
    activity_id = _activity(user_id, [200.0] * 10)
    conn = db.connect()
    with pytest.raises(sqlite3.IntegrityError, match="ownership mismatch"):
        conn.execute(
            "INSERT INTO power_sample_corrections "
            "(user_id, activity_id, start_index, end_index, ftp_basis, created) "
            "VALUES (?, ?, 0, 0, 250, 'now')",
            (other, activity_id),
        )
    conn.execute(
        "INSERT INTO power_sample_corrections "
        "(user_id, activity_id, start_index, end_index, ftp_basis, created) "
        "VALUES (?, ?, 0, 0, 250, 'now')",
        (user_id, activity_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="ownership mismatch"):
        conn.execute(
            "UPDATE power_sample_corrections SET user_id = ?",
            (other,),
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM power_sample_corrections"
    ).fetchone()[0] == 1
    conn.close()


def test_routes_require_auth_and_apply_current_users_candidate():
    with TestClient(create_app()) as client:
        assert client.get(
            "/profile/power-corrections", follow_redirects=False
        ).status_code == 303
        client.post(
            "/register", data={"username": "web", "password": "password123"}
        )
        uid = db.get_user_by_username("web")["id"]
        activity_id = _activity(uid, [100.0, 2000.0, 100.0])
        page = client.get("/profile/power-corrections?threshold=1000")
        assert page.status_code == 200
        assert "2000.0 W" in page.text
        assert "2026-07-01T10:00:01" in page.text
        response = client.post(
            "/profile/power-corrections/apply",
            data={
                "activity_id": activity_id,
                "start_index": 1,
                "end_index": 1,
                "reason": "meter spike",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert db.get_activity(uid, activity_id)["streams"]["power"][1] is None
        assert "Correct power data" in client.get("/profile").text


def test_routes_skip_malformed_history_and_enforce_exact_same_origin():
    with TestClient(create_app()) as client:
        client.post(
            "/register", data={"username": "origin", "password": "password123"}
        )
        uid = db.get_user_by_username("origin")["id"]
        db.insert_activity(uid, {
            "dedup_hash": "web-malformed",
            "filename": "malformed.fit",
            "start_time": "2026-07-01T09:00:00",
            "duration_s": 1,
            "streams": 42,
        })
        activity_id = db.insert_activity(uid, {
            "dedup_hash": "web-overflow",
            "filename": "overflow.fit",
            "start_time": "2026-07-01T10:00:00",
            "duration_s": 3,
            "np": 250.0,
            "if_": 1.0,
            "streams": {"power": [100, 10**1000, 2000]},
        })
        page = client.get("/profile/power-corrections?threshold=1000")
        assert page.status_code == 200
        assert "Results stop after 200" in page.text
        payload = {
            "activity_id": activity_id,
            "start_index": 2,
            "end_index": 2,
        }
        rejected = client.post(
            "/profile/power-corrections/apply",
            data=payload,
            headers={
                "host": "localhost:8000",
                "origin": "http://localhost:8001",
            },
            follow_redirects=False,
        )
        assert rejected.status_code == 403
        assert db.list_power_corrections(uid) == []

        accepted = client.post(
            "/profile/power-corrections/apply",
            data=payload,
            headers={
                "host": "localhost:8000",
                "origin": "http://localhost:8000",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        correction_id = db.list_power_corrections(uid, active_only=True)[0]["id"]
        rejected_undo = client.post(
            "/profile/power-corrections/undo",
            data={"correction_id": correction_id},
            headers={
                "host": "localhost:8000",
                "origin": "http://localhost:8001",
            },
            follow_redirects=False,
        )
        assert rejected_undo.status_code == 403
        assert len(db.list_power_corrections(uid, active_only=True)) == 1
        assert client.post(
            "/profile/power-corrections/undo",
            data={"correction_id": correction_id},
            follow_redirects=False,
        ).status_code == 303
