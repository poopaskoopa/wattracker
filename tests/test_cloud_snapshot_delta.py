"""Durable changed-object publication for the optional cloud sync plane."""

import json
import sqlite3

import pytest

from wattracker import db
from wattracker.cloud.client import CloudSyncClient, SyncCredentials
from wattracker.cloud.models import CloudObject, ModelError
from wattracker.cloud.storage import MemoryTenantStore
from wattracker.cloud import snapshot as snapshot_module
from wattracker.cloud.snapshot import (
    MAX_SYNC_REQUEST_BYTES,
    SnapshotError,
    _batch_wire_size,
    clear_snapshot_publication,
    commit_snapshot_batch,
    snapshot_batch,
)


def _activity(path, user_id, number, start_time):
    return db.insert_activity(
        user_id,
        {
            "dedup_hash": f"delta-{number}",
            "filename": f"ride-{number}.fit",
            "start_time": start_time,
            "duration_s": 60,
            "distance_m": 1000.0,
            "avg_power": 180.0,
            "avg_hr": 140.0,
            "np": 185.0,
            "if_": 0.75,
            "tss": 1.0,
        },
        path=str(path),
    )


def _fixture_db(tmp_path, count=1):
    path = tmp_path / "delta.db"
    db.init_db(str(path))
    user_id = db.create_user("rider", "not-a-password", path=str(path))
    db.save_user_settings(
        user_id, {"ftp": 240, "timezone": "UTC"}, path=str(path),
    )
    for number in range(1, count + 1):
        _activity(path, user_id, number, f"2026-08-{number:02d}T10:00:00")
    return path, user_id


def test_v34_migrates_in_place_and_creates_publication_tables(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    activity = db.get_activity(user_id, 1, path=str(path))
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE cloud_publication_pending")
        conn.execute("DROP TABLE cloud_publication_ledger")
        conn.execute("PRAGMA user_version = 34")
        conn.commit()
    finally:
        conn.close()

    db.init_db(str(path))

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()
    assert {
        "cloud_publication_ledger", "cloud_publication_pending",
        "cloud_publication_state",
    } <= tables
    assert db.get_activity(user_id, 1, path=str(path)) == activity


def test_delta_is_deterministic_noop_after_commit_and_supports_republish(tmp_path):
    path, user_id = _fixture_db(tmp_path)

    first = snapshot_batch(path, user_id, include_derived=False)
    assert first is not None
    retry = snapshot_batch(path, user_id, include_derived=False)
    assert retry is not None
    assert retry.batch_id == first.batch_id
    assert retry.revision == first.revision
    assert [obj.wire() for obj in retry.objects] == [
        obj.wire() for obj in first.objects
    ]

    commit_snapshot_batch(path, user_id, first)
    assert snapshot_batch(path, user_id, include_derived=False) is None

    forced = snapshot_batch(
        path, user_id, include_derived=False, republish=True,
    )
    assert forced is not None
    assert forced.revision > first.revision
    assert [obj.object_id for obj in forced.objects] == ["activity-1"]
    commit_snapshot_batch(path, user_id, forced)
    assert snapshot_batch(path, user_id, include_derived=False) is None

    clear_snapshot_publication(path, user_id)
    assert snapshot_batch(path, user_id, include_derived=False) is not None


def test_changed_object_and_local_deletion_emit_one_delta_each(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=2)
    initial = snapshot_batch(path, user_id, include_derived=False)
    assert initial is not None
    commit_snapshot_batch(path, user_id, initial)

    conn = db.connect(str(path))
    try:
        conn.execute(
            "UPDATE activities SET avg_power = 210 WHERE user_id = ? AND id = 1",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()
    changed = snapshot_batch(path, user_id, include_derived=False)
    assert changed is not None
    assert [obj.object_id for obj in changed.objects] == ["activity-1"]
    assert not changed.objects[0].deleted
    commit_snapshot_batch(path, user_id, changed)
    assert snapshot_batch(path, user_id, include_derived=False) is None

    assert db.delete_activity(user_id, 2, path=str(path)) == "deleted"
    tombstone = snapshot_batch(path, user_id, include_derived=False)
    assert tombstone is not None
    assert [obj.object_id for obj in tombstone.objects] == ["activity-2"]
    assert tombstone.objects[0].deleted
    assert tombstone.objects[0].data == {}
    commit_snapshot_batch(path, user_id, tombstone)
    assert snapshot_batch(path, user_id, include_derived=False) is None


def test_explicit_paged_batches_keep_their_identity_and_revision(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=2)
    page_one = snapshot_batch(
        path, user_id, batch_id="page-one", revision=7, limit=1,
        include_derived=False, offset=0,
    )
    assert page_one is not None
    commit_snapshot_batch(path, user_id, page_one)
    page_two = snapshot_batch(
        path, user_id, batch_id="page-two", revision=8, limit=1,
        include_derived=False, offset=0,
    )
    assert page_two is not None
    assert [obj.object_id for obj in page_two.objects] == ["activity-2"]
    retry_page_two = snapshot_batch(
        path, user_id, batch_id="page-two", revision=8, limit=1,
        include_derived=False, offset=0,
    )
    assert retry_page_two is not None
    assert [obj.object_id for obj in retry_page_two.objects] == ["activity-2"]
    commit_snapshot_batch(path, user_id, page_two)
    assert snapshot_batch(path, user_id, include_derived=False) is None


def test_generated_batches_stay_below_cloud_request_limit(tmp_path, monkeypatch):
    path, user_id = _fixture_db(tmp_path)
    objects = [
        CloudObject(
            object_id=f"load-point-{index}",
            kind="load_point",
            revision=1,
            data={"values": [0] * 16_000},
        )
        for index in range(300)
    ]
    monkeypatch.setattr(
        snapshot_module, "_all_snapshot_objects",
        lambda *_args, **_kwargs: objects,
    )
    batch = snapshot_batch(path, user_id, include_derived=False)
    assert batch is not None
    assert len(batch.objects) < len(objects)
    assert _batch_wire_size(
        batch.batch_id, batch.revision, batch.objects,
    ) < MAX_SYNC_REQUEST_BYTES
    clear_snapshot_publication(path, user_id)
    smaller = snapshot_batch(
        path, user_id, include_derived=False, limit=1,
    )
    assert smaller is not None
    assert len(smaller.objects) == 1


def test_exact_request_limit_is_rejected(tmp_path, monkeypatch):
    path, user_id = _fixture_db(tmp_path)
    objects = [CloudObject(
        object_id="activity-1", kind="activity", revision=1, data={},
    )]
    monkeypatch.setattr(
        snapshot_module, "_all_snapshot_objects",
        lambda *_args, **_kwargs: objects,
    )
    monkeypatch.setattr(
        snapshot_module, "_batch_wire_size",
        lambda *_args, **_kwargs: (
            MAX_SYNC_REQUEST_BYTES - snapshot_module._object_wire_size(objects[0])
        ),
    )
    with pytest.raises(SnapshotError, match="request limit"):
        snapshot_batch(path, user_id, include_derived=False)


def test_interrupted_push_resumes_pages_without_skipping_or_duplicate_successes(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=3)
    outcomes = [
        (200, b'{"revision":1,"replayed":false}'),
        (503, b'{"detail":"offline"}'),
        (200, b'{"revision":2,"replayed":false}'),
        (200, b'{"revision":3,"replayed":false}'),
    ]
    bodies = []

    def transport(_url, _headers, body):
        bodies.append(json.loads(body.decode("utf-8")))
        return outcomes.pop(0)

    credentials = SyncCredentials(
        "c" * 64, "subscription", b"signing-key", namespace="ab" * 32,
    )
    client = CloudSyncClient(
        "https://cloud.example", credentials, transport=transport,
        clock=lambda: 100,
    )

    first = client.push_snapshot(
        str(path), user_id, limit=1, include_derived=False,
    )
    assert [result.status_code for result in first] == [200, 503]
    second = client.push_snapshot(
        str(path), user_id, limit=1, include_derived=False,
    )
    assert [result.status_code for result in second] == [200, 200]
    assert client.push_snapshot(
        str(path), user_id, limit=1, include_derived=False,
    ) == []
    assert [body["objects"][0]["id"] for body in bodies] == [
        "activity-1", "activity-2", "activity-2", "activity-3",
    ]


def test_pending_batch_blocks_a_newer_different_option_prepare(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    pending = snapshot_batch(path, user_id, include_derived=True)
    assert pending is not None
    other = snapshot_batch(path, user_id, include_derived=False)
    assert other is not None
    assert other.batch_id == pending.batch_id
    assert other.revision == pending.revision

    retry = snapshot_batch(path, user_id, include_derived=True)
    assert retry is not None
    assert retry.batch_id == pending.batch_id
    assert [obj.wire() for obj in retry.objects] == [
        obj.wire() for obj in pending.objects
    ]

    with pytest.raises(SnapshotError, match="another publication batch"):
        snapshot_batch(
            path, user_id, include_derived=True, republish=True,
        )


def test_clear_publication_retains_server_revision_floor(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    store = MemoryTenantStore()
    first = snapshot_batch(path, user_id, include_derived=False)
    assert first is not None
    store.apply("namespace", "rider", first)
    commit_snapshot_batch(path, user_id, first)
    clear_snapshot_publication(path, user_id)

    republished = snapshot_batch(path, user_id, include_derived=False)
    assert republished is not None
    assert republished.revision > first.revision
    store.apply("namespace", "rider", republished)


def test_invalid_explicit_batch_is_not_persisted(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    with pytest.raises(ModelError, match="invalid batch id"):
        snapshot_batch(
            path, user_id, batch_id="invalid batch id", revision=1,
            include_derived=False,
        )

    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM cloud_publication_pending WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_client_republish_preserves_an_unresolved_pending_batch(tmp_path):
    path, user_id = _fixture_db(tmp_path)
    pending = snapshot_batch(path, user_id, include_derived=False)
    assert pending is not None
    credentials = SyncCredentials(
        "c" * 64, "subscription", b"signing-key", namespace="ab" * 32,
    )
    bodies = []

    def transport(_url, _headers, body):
        bodies.append(body)
        return 503, b'{"detail":"offline"}'

    client = CloudSyncClient(
        "https://cloud.example", credentials, transport=transport,
    )
    result = client.push_snapshot(
        str(path), user_id, include_derived=False, republish=True,
    )
    assert len(result) == 1
    assert result[0].status_code is None
    assert bodies == []
    retry = snapshot_batch(path, user_id, include_derived=False)
    assert retry is not None
    assert retry.batch_id == pending.batch_id
