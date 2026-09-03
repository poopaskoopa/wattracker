"""The ``profile`` object kind: publishing an FTP and reading it back.

This is the payload half of the walking skeleton (#171).  The signed-request
half is exercised by ``tests/test_canonical_request_vectors.py`` and by the
existing device-pairing tests; here the question is only whether the number
the desktop database holds is the number a reader context gets back.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.security import (
    canonical_request,
    digest_body,
    new_installation_id,
    sign_request,
)
from wattracker.cloud.snapshot import (
    PROFILE_KIND,
    PROFILE_OBJECT_ID,
    SnapshotError,
    profile_batch,
    profile_object,
)

SECRET = b"cloud-test-server-secret-32-bytes-long"

READER_HEADERS = {
    "X-Verified-Entra-Subject": "entra-user",
    "X-APIM-Request-Verified": "true",
    "X-APIM-Client-Certificate-Verified": "true",
}


# ---------------------------------------------------------------------------
# A local database that looks like the rider's, without being it
# ---------------------------------------------------------------------------


def _local_db(tmp_path, *, override=None, history=()):
    """A minimal stand-in for the two tables the profile read touches.

    Deliberately not built through ``wattracker.db``: this asserts what the
    snapshot reader does with the columns it finds, and building the whole
    schema would hide a column rename behind a passing test.
    """
    path = tmp_path / "local.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE user_settings (user_id INTEGER PRIMARY KEY, ftp REAL)"
        )
        conn.execute(
            "CREATE TABLE ftp_history ("
            "user_id INTEGER NOT NULL, date TEXT NOT NULL, "
            "ftp_watts REAL NOT NULL, source TEXT NOT NULL DEFAULT 'estimated', "
            "PRIMARY KEY(user_id, date))"
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, ftp) VALUES (?, ?)", (5, override)
        )
        conn.executemany(
            "INSERT INTO ftp_history (user_id, date, ftp_watts, source) "
            "VALUES (?, ?, ?, ?)",
            [(5, date, watts, source) for date, watts, source in history],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_profile_object_prefers_the_riders_own_override(tmp_path):
    path = _local_db(
        tmp_path, override=248.0, history=[("2026-08-28", 211.4, "ramp_test")]
    )
    obj = profile_object(path, 5)
    assert obj is not None
    assert obj.kind == PROFILE_KIND
    assert obj.object_id == PROFILE_OBJECT_ID
    assert obj.data == {"ftp_watts": 248.0}


def test_profile_object_falls_back_to_the_newest_history_row(tmp_path):
    path = _local_db(
        tmp_path,
        override=None,
        history=[
            ("2026-08-26", 209.0, "manual"),
            ("2026-08-28", 211.4, "ramp_test"),
            ("2026-08-27", 209.0, "manual"),
        ],
    )
    obj = profile_object(path, 5)
    assert obj is not None
    # Newest by date, not by insertion order.
    assert obj.data == {"ftp_watts": 211.4}


def test_a_rider_with_no_ftp_publishes_nothing_rather_than_a_default(tmp_path):
    path = _local_db(tmp_path, override=None, history=[])
    assert profile_object(path, 5) is None
    assert profile_batch(path, 5, batch_id="profile-1", revision=1) is None


@pytest.mark.parametrize("bad", [0.0, -10.0, float("nan"), float("inf")])
def test_unusable_override_values_fall_through_instead_of_being_published(
    tmp_path, bad
):
    path = _local_db(
        tmp_path, override=bad, history=[("2026-08-28", 211.4, "ramp_test")]
    )
    obj = profile_object(path, 5)
    assert obj is not None
    assert obj.data == {"ftp_watts": 211.4}


def test_profile_read_never_opens_the_database_writable(tmp_path):
    # ``readonly_connection`` refuses to create a database, so a wrong path is
    # an error rather than a new empty file next to the rider's real one.
    missing = tmp_path / "not-there.db"
    with pytest.raises(SnapshotError):
        profile_object(missing, 5)
    assert not missing.exists()


def test_profile_object_rejects_a_non_positive_user_id(tmp_path):
    path = _local_db(tmp_path, override=250.0)
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            profile_object(path, bad)


# ---------------------------------------------------------------------------
# End to end through the real sync and read planes
# ---------------------------------------------------------------------------


@pytest.fixture()
def cloud():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    return config, state, TestClient(create_cloud_app(config, state=state))


def _writer_headers(writer, body, *, revision, batch_id, nonce):
    canonical = canonical_request(
        "POST", "/api/v1/sync/batches", writer.namespace, 1_000, nonce,
        digest_body(body), batch_id, str(revision),
    )
    return {
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
        "X-Writer-Credential": writer.credential_id,
        "X-Writer-Timestamp": "1000",
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": batch_id,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": sign_request(writer.signing_key, canonical),
    }


def _publish(client, state, writer, batch):
    body = json.dumps(
        {
            "batch_id": batch.batch_id,
            "revision": batch.revision,
            "objects": [item.wire() for item in batch.objects],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return client.post(
        "/api/v1/sync/batches",
        headers=_writer_headers(
            writer, body, revision=batch.revision, batch_id=batch.batch_id,
            nonce=f"nonce-{batch.revision}",
        ),
        content=body,
    )


def test_a_published_ftp_reads_back_unchanged(cloud, tmp_path):
    _config, state, client = cloud
    writer = state.credentials.register_writer(
        new_installation_id(), "scope", b"w" * 32, b"k" * 32
    )
    path = _local_db(
        tmp_path, override=None, history=[("2026-08-28", 211.4, "ramp_test")]
    )
    batch = profile_batch(path, 5, batch_id="profile-1", revision=1)
    assert batch is not None
    assert _publish(client, state, writer, batch).status_code == 200

    token, _context = state.credentials.issue_reader_context_for_scope(
        writer.namespace, writer.local_user_scope, None
    )
    response = client.get(
        "/api/v1/context/profile",
        headers={**READER_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "id": "profile",
                "kind": "profile",
                "revision": 1,
                "data": {"ftp_watts": 211.4},
            }
        ]
    }


def test_the_profile_route_returns_only_profile_objects(cloud, tmp_path):
    _config, state, client = cloud
    writer = state.credentials.register_writer(
        new_installation_id(), "scope", b"w" * 32, b"k" * 32
    )
    path = _local_db(
        tmp_path, override=None, history=[("2026-08-28", 211.4, "ramp_test")]
    )
    batch = profile_batch(path, 5, batch_id="profile-1", revision=1)
    assert _publish(client, state, writer, batch).status_code == 200
    activity = json.dumps(
        {
            "batch_id": "activity-batch",
            "revision": 2,
            "objects": [
                {"id": "activity-1", "kind": "activity", "revision": 1,
                 "data": {"duration_s": 10}}
            ],
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()
    assert client.post(
        "/api/v1/sync/batches",
        headers=_writer_headers(
            writer, activity, revision=2, batch_id="activity-batch", nonce="nonce-2"
        ),
        content=activity,
    ).status_code == 200

    token, _context = state.credentials.issue_reader_context_for_scope(
        writer.namespace, writer.local_user_scope, None
    )
    headers = {**READER_HEADERS, "Authorization": f"Bearer {token}"}
    profile = client.get("/api/v1/context/profile", headers=headers).json()
    assert [item["kind"] for item in profile["items"]] == ["profile"]
    activities = client.get("/api/v1/context/activities", headers=headers).json()
    assert [item["kind"] for item in activities["items"]] == ["activity"]


def test_the_profile_route_refuses_an_anonymous_reader(cloud):
    _config, _state, client = cloud
    assert client.get("/api/v1/context/profile").status_code == 404
    assert client.get(
        "/api/v1/context/profile",
        headers={**READER_HEADERS, "Authorization": "Bearer not-a-context"},
    ).status_code == 404


def test_a_profile_is_scoped_to_the_context_that_asks_for_it(cloud, tmp_path):
    """A second rider's reader context must not see the first one's FTP."""
    _config, state, client = cloud
    writer = state.credentials.register_writer(
        new_installation_id(), "scope", b"w" * 32, b"k" * 32
    )
    path = _local_db(
        tmp_path, override=None, history=[("2026-08-28", 211.4, "ramp_test")]
    )
    batch = profile_batch(path, 5, batch_id="profile-1", revision=1)
    assert _publish(client, state, writer, batch).status_code == 200

    other_token, _other = state.credentials.issue_reader_context(
        new_installation_id(), "other-scope", None
    )
    response = client.get(
        "/api/v1/context/profile",
        headers={**READER_HEADERS, "Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_the_context_capability_list_advertises_profile(cloud):
    _config, state, client = cloud
    token, _context = state.credentials.issue_reader_context(
        new_installation_id(), "scope", None
    )
    payload = client.get(
        "/api/v1/context",
        headers={**READER_HEADERS, "Authorization": f"Bearer {token}"},
    ).json()
    assert payload["capabilities"]["profile"] is True
