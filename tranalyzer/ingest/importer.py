"""Scan and import .fit activities into the database (idempotent, per-user)."""
from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import os
import tempfile
from typing import Dict, List, Optional

import numpy as np

from .. import db
from ..metrics.power import (
    estimate_ftp,
    intensity_factor,
    normalized_power,
    training_stress_score,
)
from ..paths import activities_dir
from .fit_parser import parse_fit


def dedup_hash(start_time: Optional[str], duration_s: int) -> str:
    """Stable hash from (start_time, duration) to dedupe activities."""
    key = f"{start_time or 'unknown'}|{duration_s}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


FTP_UPDATE_DAYS = 30
FTP_ESTIMATE_WINDOW_DAYS = 42


def current_ftp(
    user_id: int,
    now: Optional[_dt.datetime] = None,
    extra_power: Optional[List[List[float]]] = None,
) -> float:
    """Resolve the current FTP for a user.

    Precedence: latest ftp_history value -> user's FTP override -> fresh estimate
    over the trailing 42 days. Falls back to a sane default if no power data.
    """
    db.init_db()
    latest = db.latest_ftp(user_id)
    if latest and latest.get("ftp_watts", 0) > 0:
        return float(latest["ftp_watts"])
    settings = db.get_user_settings(user_id)
    override = settings.get("ftp")
    if override and float(override) > 0:
        return float(override)
    activities = db.full_activities(user_id)
    streams: List = list(activities)
    if extra_power:
        streams = streams + [p for p in extra_power if p]
    ftp = estimate_ftp(streams, window_days=FTP_ESTIMATE_WINDOW_DAYS, now=now)
    return ftp if ftp > 0 else 200.0


# Backwards-compatible alias.
def resolve_ftp(user_id: int, extra_power: Optional[List[List[float]]] = None) -> float:
    return current_ftp(user_id, extra_power=extra_power)


def ftp_update_due(user_id: int, now: Optional[_dt.datetime] = None) -> bool:
    """True if >= FTP_UPDATE_DAYS since the user's last ftp_history entry.

    With no history at all, an update is due (seeds the first estimate).
    """
    db.init_db()
    now = now or _dt.datetime.now()
    latest = db.latest_ftp(user_id)
    if not latest:
        return True
    try:
        last_date = _dt.date.fromisoformat(latest["date"])
    except (ValueError, TypeError):
        return True
    return (now.date() - last_date).days >= FTP_UPDATE_DAYS


def maybe_update_ftp(user_id: int, now: Optional[_dt.datetime] = None) -> bool:
    """Append a fresh estimated FTP row for the user if a monthly update is due.

    Estimates over the trailing 42 days of the user's activities. Only appends
    when due and when a positive estimate is available; never overwrites a manual
    entry. Returns True if a row was appended.
    """
    db.init_db()
    now = now or _dt.datetime.now()
    if not ftp_update_due(user_id, now):
        return False
    est = estimate_ftp(
        db.full_activities(user_id), window_days=FTP_ESTIMATE_WINDOW_DAYS, now=now
    )
    if est <= 0:
        return False
    db.add_ftp_entry(user_id, now.date().isoformat(), round(est, 1), "estimated")
    return True


def _mean(vals) -> float:
    arr = np.array([0.0 if v is None else float(v) for v in vals], dtype=float)
    arr = np.nan_to_num(arr, nan=0.0)
    return float(arr.mean()) if arr.size else 0.0


def _build_record(parsed: Dict, filename: str, ftp: float) -> Dict:
    streams = parsed["streams"]
    power = streams.get("power") or []
    hr = streams.get("heartrate") or []
    distance = streams.get("distance") or []
    duration_s = parsed["duration_s"]

    npw = normalized_power(power) if power else 0.0
    ifv = intensity_factor(npw, ftp) if ftp > 0 else 0.0
    tss = training_stress_score(duration_s, npw, ftp) if ftp > 0 else 0.0
    dist_m = 0.0
    clean_dist = [d for d in distance if d is not None]
    if clean_dist:
        dist_m = float(max(clean_dist))

    return {
        "dedup_hash": dedup_hash(parsed["start_time"], duration_s),
        "filename": filename,
        "start_time": parsed["start_time"],
        "duration_s": duration_s,
        "distance_m": dist_m,
        "avg_power": _mean(power) if power else 0.0,
        "avg_hr": _mean(hr) if hr else 0.0,
        "np": round(npw, 1),
        "if_": round(ifv, 3),
        "tss": round(tss, 1),
        "streams": streams,
    }


def ingest_file(user_id: int, path: str, ftp: Optional[float] = None) -> Optional[int]:
    """Parse and store a single .fit file for a user. Returns id or None if dup."""
    db.init_db()
    parsed = parse_fit(path)
    h = dedup_hash(parsed["start_time"], parsed["duration_s"])
    if db.activity_exists(user_id, h):
        return None
    if ftp is None:
        ftp = current_ftp(
            user_id, extra_power=[parsed["streams"].get("power") or []]
        )
    record = _build_record(parsed, os.path.basename(path), ftp)
    return db.insert_activity(user_id, record)


def _user_activities_dir(user_id: int) -> Optional[str]:
    override = db.get_user_settings(user_id).get("activities_dir")
    return activities_dir(override=override)


def scan_activities(user_id: int, directory: Optional[str] = None) -> Dict[str, int]:
    """Scan a user's Activities directory for .fit files and import new ones.

    Idempotent: already-imported activities are skipped.
    """
    db.init_db()
    directory = directory or _user_activities_dir(user_id)
    found = 0
    imported = 0
    skipped = 0
    if not directory or not os.path.isdir(directory):
        return {"found": 0, "imported": 0, "skipped": 0, "directory": directory}

    ftp = current_ftp(user_id)

    files: List[str] = []
    for pat in ("*.fit", "*.FIT"):
        files.extend(glob.glob(os.path.join(directory, pat)))
    for path in sorted(set(files)):
        found += 1
        try:
            new_id = ingest_file(user_id, path, ftp=ftp)
        except Exception:
            skipped += 1
            continue
        if new_id is None:
            skipped += 1
        else:
            imported += 1

    maybe_update_ftp(user_id)

    return {
        "found": found,
        "imported": imported,
        "skipped": skipped,
        "directory": directory,
    }


def ingest_upload(user_id: int, filename: str, content: bytes) -> Optional[int]:
    """Ingest an uploaded .fit file (raw bytes) for a user."""
    suffix = os.path.splitext(filename)[1] or ".fit"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(content)
        tmp.flush()
        tmp.close()
        result = ingest_file(user_id, tmp.name)
        maybe_update_ftp(user_id)
        return result
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
