"""Read-only local SQLite snapshots for optional cloud synchronization."""
from __future__ import annotations

import os
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import quote

from ..db import _correction_ranges, _effective_stream_mapping
from .models import CloudObject, MAX_BATCH_OBJECTS, ModelError, SyncBatch


class SnapshotError(RuntimeError):
    """The local database could not be opened as a read-only snapshot."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _correction_schema_available(conn: sqlite3.Connection) -> bool:
    required = {"activity_id", "user_id", "start_index", "end_index", "undone_at"}
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(power_sample_corrections)")
    }
    return required <= columns


@contextmanager
def readonly_connection(path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    """Open the live DB read-only on a separate SQLite connection.

    The cloud path never shares the request connection or changes local
    schema/data.  SQLite's URI mode also prevents accidental database creation
    when a configured path is wrong.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SnapshotError("local database does not exist")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise SnapshotError("could not open local database read-only") from exc
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def snapshot_counts(path: str | os.PathLike[str], user_id: int) -> dict[str, int]:
    """Return bounded integrity counts without exposing local identifiers."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be positive")
    with readonly_connection(path) as conn:
        result: dict[str, int] = {}
        for table in ("activities", "streams", "race_results", "plan_workouts"):
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?",  # table is constant
                    (user_id,),
                ).fetchone()
            except sqlite3.Error:
                result[table] = 0
            else:
                result[table] = int(row["n"] if row else 0)
        return result


def snapshot_objects(
    path: str | os.PathLike[str],
    user_id: int,
    *,
    limit: int = MAX_BATCH_OBJECTS,
    include_streams: bool = False,
) -> list[CloudObject]:
    """Read a bounded, user-scoped object snapshot on a separate connection.

    Local row IDs are used only as opaque object IDs within the enrolled scope;
    the serialized data contains no local user ID, filename, storage path, or
    database credential. Streams are opt-in and remain bounded by the model.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be positive")
    if limit < 1 or limit > MAX_BATCH_OBJECTS:
        raise ValueError("limit is out of bounds")
    result: list[CloudObject] = []
    with readonly_connection(path) as conn:
        activity_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(activities)")
        }
        duplicate_filter = (
            " AND duplicate_of IS NULL" if "duplicate_of" in activity_columns else ""
        )
        rows = conn.execute(
            "SELECT id, start_time, duration_s, distance_m, avg_power, avg_hr, "
            "np, if_, tss, rpe, streams FROM activities "
            f"WHERE user_id = ?{duplicate_filter} ORDER BY id LIMIT ?",
            (user_id, limit),
        ).fetchall()
        ranges = {}
        if include_streams and _correction_schema_available(conn):
            ranges = _correction_ranges(
                conn, user_id, [int(row["id"]) for row in rows]
            )
        for row in rows:
            data = {
                key: row[key]
                for key in (
                    "start_time", "duration_s", "distance_m", "avg_power",
                    "avg_hr", "np", "if_", "tss", "rpe",
                )
                if row[key] is not None
            }
            if include_streams and row["streams"] is not None:
                # Decode only for an explicitly requested upload. The model's
                # 512 KiB object limit prevents a single stream from becoming
                # an unbounded request body.
                import zlib

                try:
                    decoder = zlib.decompressobj()
                    decoded_parts: list[bytes] = []
                    decoded_size = 0
                    compressed = row["streams"]
                    for start in range(0, len(compressed), 64 * 1024):
                        part = decoder.decompress(
                            compressed[start : start + 64 * 1024],
                            512 * 1024 - decoded_size + 1,
                        )
                        decoded_parts.append(part)
                        decoded_size += len(part)
                        if decoded_size > 512 * 1024:
                            raise ValueError("stream snapshot is too large")
                    part = decoder.flush(512 * 1024 - decoded_size + 1)
                    decoded_parts.append(part)
                    decoded_size += len(part)
                    if (
                        decoded_size > 512 * 1024
                        or not decoder.eof
                        or decoder.unused_data
                        or decoder.unconsumed_tail
                    ):
                        raise ValueError("stream snapshot is invalid or too large")
                    decoded = b"".join(decoded_parts)
                    stream_data = json.loads(
                        decoded.decode("utf-8"), parse_constant=_reject_json_constant
                    )
                    if isinstance(stream_data, dict):
                        stream_data = _effective_stream_mapping(
                            stream_data, ranges.get(int(row["id"]), [])
                        )
                    if len(
                        json.dumps(stream_data, separators=(",", ":")).encode()
                    ) <= 512 * 1024:
                        data["streams"] = stream_data
                except (
                    TypeError, ValueError, UnicodeDecodeError, zlib.error, RecursionError
                ):
                    pass
            try:
                obj = CloudObject(
                    object_id=f"activity-{int(row['id'])}",
                    kind="activity",
                    revision=max(1, int(row["id"])),
                    data=data,
                )
            except ModelError:
                if "streams" not in data:
                    raise
                data.pop("streams")
                obj = CloudObject(
                    object_id=f"activity-{int(row['id'])}",
                    kind="activity",
                    revision=max(1, int(row["id"])),
                    data=data,
                )
            result.append(obj)
    return result


def snapshot_digest(objects: list[CloudObject]) -> str:
    """Return a stable integrity hash for a read-only snapshot."""
    material = json.dumps(
        [obj.wire() for obj in sorted(objects, key=lambda item: item.object_id)],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def snapshot_batch(
    path: str | os.PathLike[str],
    user_id: int,
    *,
    batch_id: str,
    revision: int,
    limit: int = MAX_BATCH_OBJECTS,
    include_streams: bool = False,
) -> SyncBatch:
    """Build one bounded sync batch without mutating the local database."""
    return SyncBatch(
        batch_id=batch_id,
        revision=revision,
        objects=tuple(
            snapshot_objects(
                path, user_id, limit=limit, include_streams=include_streams
            )
        ),
    )


# ---------------------------------------------------------------------------
# The ``profile`` object kind
# ---------------------------------------------------------------------------
# One object, one field: the rider's current FTP.  It exists so the walking
# skeleton (#171) has something real to carry end to end, and it is
# deliberately the smallest thing that can be.  Issue #154 owns the full
# object model, and a richer ``profile`` invented here would collide with it.
#
# Nothing else about the rider goes in.  Weight, heart-rate maxima, zones and
# provenance are all things #154 gets to shape; a number the phone renders is
# not.
PROFILE_OBJECT_ID = "profile"
PROFILE_KIND = "profile"


def _finite_positive(value: object) -> Optional[float]:
    """``value`` as a usable wattage, or ``None``.

    A NaN or an infinity would be refused by the wire model later, after the
    caller had already built a batch around it; a non-positive number is not
    an FTP.  Both are filtered here, at the read.
    """

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def _current_ftp_watts(conn: sqlite3.Connection, user_id: int) -> Optional[float]:
    """The FTP the desktop would show, read on a read-only connection.

    Precedence mirrors :func:`wattracker.ingest.importer.current_ftp` for its
    first two steps -- the rider's manual override, then the newest
    ``ftp_history`` row -- and deliberately stops there.  The third step is a
    live detraining-decayed *estimate*, which needs ``init_db()`` and a write
    connection, and an estimate is not a fact about the rider worth publishing
    to another device as though it were one.  A rider with neither a stated
    nor a recorded FTP publishes no profile at all, rather than a default that
    would arrive on the phone looking like a measurement.
    """

    try:
        row = conn.execute(
            "SELECT ftp FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row["ftp"] is not None:
        override = _finite_positive(row["ftp"])
        if override is not None:
            return override
    try:
        row = conn.execute(
            "SELECT ftp_watts FROM ftp_history WHERE user_id = ? "
            "ORDER BY date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _finite_positive(row["ftp_watts"])


def profile_object(
    path: str | os.PathLike[str], user_id: int, *, revision: int = 1
) -> Optional[CloudObject]:
    """The rider's ``profile`` object, or ``None`` when there is no FTP.

    ``revision`` is the publisher's counter, not a database row id: the
    profile is a single mutable object, so there is no local row whose
    identity could supply a version.  Callers publishing it in a batch pass
    the batch revision, which is monotonic by construction.
    """

    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be positive")
    with readonly_connection(path) as conn:
        ftp_watts = _current_ftp_watts(conn, user_id)
    if ftp_watts is None:
        return None
    return CloudObject(
        object_id=PROFILE_OBJECT_ID,
        kind=PROFILE_KIND,
        revision=revision,
        data={"ftp_watts": ftp_watts},
    )


def profile_batch(
    path: str | os.PathLike[str],
    user_id: int,
    *,
    batch_id: str,
    revision: int,
) -> Optional[SyncBatch]:
    """One batch carrying only the profile object.

    ``None`` when there is nothing to publish: a batch with no objects is
    invalid by model, so the absence is returned rather than an empty batch.
    """

    obj = profile_object(path, user_id, revision=revision)
    if obj is None:
        return None
    return SyncBatch(batch_id=batch_id, revision=revision, objects=(obj,))
