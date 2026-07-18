"""Scan and import .fit activities into the database (idempotent, per-user)."""
from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import os
import tempfile
import time
from typing import Callable, Dict, List, Optional

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


FTP_UPDATE_DAYS = 21  # re-evaluate at least every 3 weeks


def _current_estimate(
    activities: List[dict],
    now: Optional[_dt.datetime] = None,
    extra_power: Optional[List[List[float]]] = None,
) -> float:
    """FTP estimate anchored at wall-clock `now` using the detraining decay.

    Anchoring at `now` (rather than the last activity) is intentional: with the
    smooth decay model the estimate never "empties" during a layoff, it just
    decays honestly, which is the desired behavior - a rider genuinely loses
    fitness across a break.
    """
    now = now or _dt.datetime.now()
    streams: List = list(activities)
    if extra_power:
        streams = streams + [p for p in extra_power if p]
    return estimate_ftp(streams, now=now)


def current_ftp(
    user_id: int,
    now: Optional[_dt.datetime] = None,
    extra_power: Optional[List[List[float]]] = None,
) -> float:
    """Resolve the current FTP for a user.

    Precedence: latest ftp_history value -> user's FTP override -> fresh
    detraining-decayed estimate anchored at `now`. Falls back to a sane default
    if no power data.
    """
    db.init_db()
    latest = db.latest_ftp(user_id)
    if latest and latest.get("ftp_watts", 0) > 0:
        return float(latest["ftp_watts"])
    settings = db.get_user_settings(user_id)
    override = settings.get("ftp")
    if override and float(override) > 0:
        return float(override)
    ftp = _current_estimate(db.full_activities(user_id), now, extra_power)
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


def evaluate_ftp(user_id: int, now: Optional[_dt.datetime] = None) -> bool:
    """Record/refresh the user's estimated FTP so history tracks evaluations.

    - When an update is due (>= FTP_UPDATE_DAYS since the last row, or no
      history), a new dated 'estimated' row is appended.
    - Otherwise, if the LATEST row is 'estimated' but disagrees with the current
      evaluation (e.g. it was recorded with a mis-anchored window, or new rides
      changed the picture), its value is refreshed in place - so `current_ftp`
      always reflects the most recent evaluation. Manual rows are never touched.

    The estimate is the detraining-decayed value anchored at `now`
    (see _current_estimate). Returns True when a row was appended or refreshed.
    """
    db.init_db()
    now = now or _dt.datetime.now()
    est = _current_estimate(db.full_activities(user_id), now)
    if est <= 0:
        return False
    est = round(est, 1)
    latest = db.latest_ftp(user_id)
    if latest is None or ftp_update_due(user_id, now):
        db.add_ftp_entry(user_id, now.date().isoformat(), est, "estimated")
        return True
    if latest["source"] == "estimated" and abs(float(latest["ftp_watts"]) - est) >= 0.1:
        return db.update_estimated_ftp_entry(user_id, latest["date"], est)
    return False


# Backwards-compatible alias (older call sites / tests).
maybe_update_ftp = evaluate_ftp


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
    # Same ride can land in two files with an identical start second but a
    # slightly different duration (Zwift's in-progress temp file vs. the final
    # timestamped .fit), so the dedup_hash alone won't catch it - dedup on the
    # exact start_time too.
    if parsed["start_time"] is not None and db.activity_exists_by_start(
        user_id, parsed["start_time"]
    ):
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


def scan_activities(
    user_id: int,
    directory: Optional[str] = None,
    progress: Optional[Callable[[dict], None]] = None,
) -> Dict[str, int]:
    """Scan a user's Activities directory for .fit files and import new ones.

    Fast incremental rescan: files already recorded in ``scanned_files`` with an
    unchanged mtime+size are skipped WITHOUT parsing. Every file that is
    parsed - whether newly imported or a duplicate - is recorded so it is never
    parsed again (changed mtime/size re-processes and refreshes the row).

    ``progress`` (optional) is called with incremental field updates so a caller
    can surface live status: once with ``{"total": N}`` after globbing, then
    after each file with ``processed``/``imported``/``skipped`` counts.
    """
    db.init_db()
    directory = directory or _user_activities_dir(user_id)
    found = 0
    imported = 0
    skipped = 0

    def _report(**fields):
        if progress:
            progress(fields)

    if not directory or not os.path.isdir(directory):
        _report(total=0, processed=0, imported=0, skipped=0)
        return {"found": 0, "imported": 0, "skipped": 0, "completed": 0,
                "directory": directory}

    ftp = current_ftp(user_id)

    files: List[str] = []
    for pat in ("*.fit", "*.FIT"):
        files.extend(glob.glob(os.path.join(directory, pat)))
    ordered = sorted(set(files))
    _report(total=len(ordered), processed=0, imported=0, skipped=0)

    seen = db.seen_files(user_id)
    for path in ordered:
        found += 1
        # Zwift keeps `inProgressActivity.fit` as a live recording buffer while
        # a ride is being recorded; it's never a finished ride (and its start
        # second collides with the eventual final .fit). Skip it before the
        # scanned_files cache check so it's never cached or reparsed.
        if os.path.basename(path).lower() == "inprogressactivity.fit":
            skipped += 1
            _report(processed=found, imported=imported, skipped=skipped)
            continue
        try:
            st = os.stat(path)
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            skipped += 1
            _report(processed=found, imported=imported, skipped=skipped)
            continue

        prev = seen.get(path)
        if prev is not None and prev[0] == mtime and prev[1] == size:
            # Already scanned, unchanged - skip without parsing.
            skipped += 1
            _report(processed=found, imported=imported, skipped=skipped)
            continue

        try:
            new_id = ingest_file(user_id, path, ftp=ftp)
        except Exception:
            skipped += 1
            _report(processed=found, imported=imported, skipped=skipped)
            continue

        # Record whether it was a new import or a duplicate, so subsequent
        # rescans skip it without parsing.
        db.record_scanned_file(user_id, path, mtime, size)
        if new_id is None:
            skipped += 1
        else:
            imported += 1
        _report(processed=found, imported=imported, skipped=skipped)
        # FIT parsing is pure-Python CPU-bound and holds the GIL; yield briefly
        # after each actually-parsed file so request threads (dashboard reads)
        # stay responsive during a long background rescan. Incrementally
        # skipped files never reach here, so the fast path is unaffected.
        time.sleep(0.01)

    # Only the (relatively expensive) post-scan work runs when something new
    # actually landed - a rescan that imported nothing changes no derived state.
    completed = 0
    if imported > 0:
        evaluate_ftp(user_id)
        completed = match_plan_completions(user_id)

    return {
        "found": found,
        "imported": imported,
        "skipped": skipped,
        "completed": completed,
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
        evaluate_ftp(user_id)
        match_plan_completions(user_id)
        return result
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ----------------------------------------------- plan-workout completion
# An activity completes a same-day plan workout when its duration or TSS is
# within this relative tolerance of the prescription.
COMPLETION_TOLERANCE = 0.30


def _completion_score(activity: dict, workout: dict) -> Optional[float]:
    """Match quality (lower is better), or None if outside tolerance.

    Simple and defensible: same user + same date is required by the caller;
    here the ride must be within +/-30% of the prescribed duration, or (when
    duration is off, e.g. a ride cut short) within +/-30% of the prescribed
    TSS. Score is the relative duration error so the closest ride wins.
    """
    plan_dur = float(workout.get("duration_s") or 0)
    act_dur = float(activity.get("duration_s") or 0)
    if plan_dur <= 0:
        return None
    dur_err = abs(act_dur - plan_dur) / plan_dur
    if dur_err <= COMPLETION_TOLERANCE:
        return dur_err
    plan_tss = float(workout.get("tss") or 0)
    act_tss = float(activity.get("tss") or 0)
    if plan_tss > 0 and abs(act_tss - plan_tss) / plan_tss <= COMPLETION_TOLERANCE:
        return COMPLETION_TOLERANCE + abs(act_tss - plan_tss) / plan_tss
    return None


def match_plan_completions(user_id: int, now: Optional[_dt.datetime] = None) -> int:
    """Mark plan workouts completed by matching same-day activities.

    For every not-yet-completed plan workout dated today or earlier, find the
    user's best-matching activity on that date (each activity completes at most
    one workout). Returns the number of workouts newly marked completed.
    """
    db.init_db()
    now = now or _dt.datetime.now()
    used = db.completed_activity_ids(user_id)
    marked = 0
    for workout in db.incomplete_plan_workouts_up_to(user_id, now.date().isoformat()):
        best = None
        best_score = None
        for act in db.activities_on_date(user_id, workout["date"]):
            if act["id"] in used:
                continue
            score = _completion_score(act, workout)
            if score is not None and (best_score is None or score < best_score):
                best, best_score = act, score
        if best is not None:
            if db.mark_plan_workout_completed(
                user_id, workout["id"], best["id"], workout["date"]
            ):
                used.add(best["id"])
                marked += 1
    return marked


def run_auto_scan(now: Optional[_dt.datetime] = None) -> Dict[str, int]:
    """One pass of the daily background job, over every known user.

    Imports new .fit files from each user's watch folder (their activities_dir
    setting, defaulting to the OS Zwift Activities folder), re-evaluates FTP,
    and matches plan-workout completions. Safe to call repeatedly (idempotent).
    """
    db.init_db()
    totals = {"users": 0, "imported": 0, "completed": 0}
    for uid in db.all_user_ids():
        totals["users"] += 1
        try:
            # scan_activities gates completion matching (and its own FTP
            # re-eval) on new imports and reports both back.
            result = scan_activities(uid)
            totals["imported"] += int(result.get("imported", 0))
            totals["completed"] += int(result.get("completed", 0))
            # Re-evaluate FTP for every user even when nothing new imported:
            # existing DBs may hold a stale collapsed 'estimated' row from the
            # old hard-window anchoring, and evaluate_ftp self-heals the latest
            # estimated row in place when it disagrees (no migration needed).
            evaluate_ftp(uid)
        except Exception:
            pass  # a broken folder for one user must not stop the sweep
    return totals
