"""Scan and import .fit activities into the database (idempotent, per-user)."""
from __future__ import annotations

import bisect
import datetime as _dt
import glob
import hashlib
import os
import tempfile
import time
import xml.etree.ElementTree as _ET
from typing import Callable, Dict, List, Optional

import numpy as np

from .. import db
from ..metrics.power import (
    estimate_ftp,
    intensity_factor,
    normalized_power,
    training_stress_score,
)
from ..metrics import profile_store
from ..paths import activities_dir
from ..timeutil import parse_naive, utc_now, utc_today
from .fit_parser import parse_fit


def dedup_hash(start_time: Optional[str], duration_s: int) -> str:
    """Stable hash from (start_time, duration) to dedupe activities."""
    key = f"{start_time or 'unknown'}|{duration_s}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


FTP_UPDATE_DAYS = 21  # re-evaluate at least every 3 weeks

# ------------------------------------------------- cross-source duplicates
# Riding with Zwift and the app running at the same time records one ride
# twice: an in-app activity and an imported .fit. Both rows are kept, but the
# in-app one is marked duplicate_of the .fit so training load counts it once.
DUPLICATE_START_TOLERANCE_S = 180
DUPLICATE_DURATION_TOLERANCE = 0.20  # the app's clock pauses when the rider does
DUPLICATE_POWER_TOLERANCE = 0.15

# Standalone workout matching and conservative RPE feedback policy.
STANDALONE_DURATION_TOLERANCE = 0.20
PROFILE_MIN_COMPLIANCE = 0.90
PROFILE_MIN_HARD_SECONDS = 180
FTP_FEEDBACK_WINDOW_DAYS = 28
FTP_FEEDBACK_MIN_WORKOUTS = 2
FTP_FEEDBACK_MAX_STEP = 0.05
FTP_RPE_STEP_PER_POINT = 0.025


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
    now = now or utc_now()
    streams: List = list(activities)
    if extra_power:
        streams = streams + [p for p in extra_power if p]
    return estimate_ftp(streams, now=now)


def recent_best_effort_ftp(
    user_id: int, now: Optional[_dt.datetime] = None
) -> float:
    """Trailing-90-day best-20-minute power * 0.95, without inactivity decay."""
    db.init_db()
    now = now or utc_now()
    cutoff = now - _dt.timedelta(days=90)
    recent = []
    for activity in db.full_activities(user_id):
        when = parse_naive(activity.get("start_time"))
        if when is not None and cutoff <= when <= now:
            recent.append(activity)
    return estimate_ftp(recent)


def current_ftp(
    user_id: int,
    now: Optional[_dt.datetime] = None,
    extra_power: Optional[List[List[float]]] = None,
) -> float:
    """Resolve the current FTP for a user.

    Precedence: user's FTP override -> latest ftp_history value -> fresh
    detraining-decayed estimate anchored at `now`. Falls back to a sane default
    if no power data.
    """
    db.init_db()
    settings = db.get_user_settings(user_id)
    override = settings.get("ftp")
    if override and float(override) > 0:
        return float(override)
    latest = db.latest_ftp(user_id)
    if latest and latest.get("ftp_watts", 0) > 0:
        return float(latest["ftp_watts"])
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
    now = now or utc_now()
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
    now = now or utc_now()
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
    new_id = db.insert_activity(user_id, record)
    if new_id is not None:
        # The rider may have recorded this same ride in-app at the same time.
        link_duplicate_activity(user_id, new_id)
    return new_id


def is_in_app_activity(filename: Optional[str]) -> bool:
    """True when an activity was recorded in-app rather than imported.

    In-app rides are named ``Ride <ISO date> <workout name>``
    (``ble/runner.py``); imported rows carry the .fit file's basename. Keep in
    step with ``db.IN_APP_FILENAME_SQL``.
    """
    name = (filename or "").strip()
    if len(name) < 17 or not name.startswith("Ride ") or name[15] != " ":
        return False
    if name.lower().endswith(".fit"):
        return False
    try:
        _dt.date.fromisoformat(name[5:15])
    except ValueError:
        return False
    return True


def _within(a: Optional[float], b: Optional[float], tolerance: float) -> bool:
    """Whether two magnitudes agree within a symmetric relative tolerance."""
    x, y = float(a or 0.0), float(b or 0.0)
    scale = max(abs(x), abs(y))
    if scale <= 0:
        return True
    return abs(x - y) / scale <= tolerance


def _same_ride(a: dict, b: dict) -> bool:
    """Whether two activity summaries are one ride recorded by both sources.

    Cross-source is a hard requirement, not a heuristic: two .fit files a few
    minutes apart with similar durations are genuinely separate rides (the
    user's history has 129 such pairs), and only the in-app/imported split
    separates them from a real double-recording.
    """
    if is_in_app_activity(a.get("filename")) == is_in_app_activity(b.get("filename")):
        return False
    start_a = parse_naive(a.get("start_time"))
    start_b = parse_naive(b.get("start_time"))
    if start_a is None or start_b is None:
        return False
    if abs((start_a - start_b).total_seconds()) > DUPLICATE_START_TOLERANCE_S:
        return False
    if not _within(a.get("duration_s"), b.get("duration_s"),
                   DUPLICATE_DURATION_TOLERANCE):
        return False
    # A ride with no power at all can't be compared on power; time and duration
    # carry the match on their own.
    if a.get("avg_power") and b.get("avg_power"):
        return _within(a["avg_power"], b["avg_power"], DUPLICATE_POWER_TOLERANCE)
    return True


def _link_pair(user_id: int, primary: dict, secondary: dict) -> Optional[int]:
    """Mark ``secondary`` a duplicate of ``primary``, carrying its data over."""
    if not db.set_duplicate_of(user_id, secondary["id"], primary["id"]):
        return None
    if not primary.get("rpe") and secondary.get("rpe"):
        db.set_activity_rpe(user_id, primary["id"], int(secondary["rpe"]))
    db.repoint_completed_activity(user_id, secondary["id"], primary["id"])
    return secondary["id"]


def find_duplicate_activity(user_id: int, activity: dict) -> Optional[dict]:
    """The other-source activity that is the same ride as ``activity``, or None."""
    when = parse_naive(activity.get("start_time"))
    if when is None:
        return None
    window = _dt.timedelta(seconds=DUPLICATE_START_TOLERANCE_S)
    candidates = []
    for other in db.activities_for_matching(
        user_id, (when - window).isoformat(), (when + window).isoformat()
    ):
        if other["id"] == activity.get("id") or other.get("duplicate_of") is not None:
            continue
        if _same_ride(activity, other):
            other_when = parse_naive(other["start_time"])
            candidates.append(
                (abs((other_when - when).total_seconds()), other["id"], other)
            )
    if not candidates:
        return None
    return min(candidates)[2]


def link_duplicate_activity(user_id: int, activity_id: int) -> Optional[int]:
    """Link a just-saved activity to the same ride recorded by the other source.

    The imported .fit is always the primary (it has distance and an unpaused
    clock); the in-app ride becomes the secondary. Returns the id of the row
    marked as a duplicate, or None when there is no counterpart.
    """
    db.init_db()
    activity = db.get_activity(user_id, activity_id)
    if not activity or activity.get("duplicate_of") is not None:
        return None
    activity["id"] = activity_id
    other = find_duplicate_activity(user_id, activity)
    if other is None:
        return None
    if is_in_app_activity(activity.get("filename")):
        return _link_pair(user_id, other, activity)
    return _link_pair(user_id, activity, other)


def backfill_duplicate_links(user_id: int) -> int:
    """Link every historical cross-source pair for a user. Safe to re-run.

    Repairs a database recorded before duplicate detection existed. Runs off a
    single pass over activity summaries (no streams inflated) with the imported
    rides indexed by start time, so a 13k-activity history stays linear.
    """
    db.init_db()
    rows = db.activities_for_matching(user_id)
    fresh = [r for r in rows if r.get("duplicate_of") is None]
    imported = sorted(
        (r for r in fresh if not is_in_app_activity(r.get("filename"))),
        key=lambda r: str(r.get("start_time") or ""),
    )
    starts = [str(r.get("start_time") or "") for r in imported]
    taken = db.primaries_with_duplicates(user_id)
    window = _dt.timedelta(seconds=DUPLICATE_START_TOLERANCE_S)
    linked = 0
    for ride in fresh:
        if not is_in_app_activity(ride.get("filename")):
            continue
        when = parse_naive(ride.get("start_time"))
        if when is None:
            continue
        best = None
        i = bisect.bisect_left(starts, (when - window).isoformat())
        while i < len(imported) and starts[i] <= (when + window).isoformat():
            candidate = imported[i]
            i += 1
            if candidate["id"] in taken or not _same_ride(ride, candidate):
                continue
            gap = abs(
                (parse_naive(candidate["start_time"]) - when).total_seconds()
            )
            if best is None or gap < best[0]:
                best = (gap, candidate)
        if best and _link_pair(user_id, best[1], ride) is not None:
            taken.add(best[1]["id"])
            linked += 1
    return linked


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
        # New rides move the rider's measured capacities (a new 5s or 5min
        # peak, a new FTP), and every prescription is built on the STORED
        # snapshot - so refresh it here, after FTP re-evaluation, rather than
        # leaving the plan quoting yesterday's rider until the nightly sweep.
        # Deliberately inside the `imported > 0` guard: nothing new landed
        # means nothing derived changed.
        profile_store.refresh(user_id)

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
        if result is not None:
            # Same reasoning as scan_activities: a new ride can move every
            # measured capacity, and prescriptions read the stored snapshot.
            profile_store.refresh(user_id)
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


def _zwo_fraction_profile(zwo_str: str) -> List[float]:
    """Expand a ZWO workout into one target-FTP fraction per second."""
    try:
        root = _ET.fromstring(zwo_str)
    except (_ET.ParseError, TypeError):
        return []
    workout = root.find("workout")
    if workout is None:
        return []
    out: List[float] = []
    for el in workout:
        tag = el.tag.lower()
        try:
            if tag in ("warmup", "cooldown", "ramp"):
                duration = int(el.attrib.get("Duration", 0))
                low = float(el.attrib.get("PowerLow", 0))
                high = float(el.attrib.get("PowerHigh", 0))
                if duration > 0:
                    out.extend(np.linspace(low, high, duration).tolist())
            elif tag == "steadystate":
                out.extend([float(el.attrib["Power"])] * int(el.attrib["Duration"]))
            elif tag == "intervalst":
                repeat = int(el.attrib.get("Repeat", 0))
                on = [float(el.attrib["OnPower"])] * int(el.attrib["OnDuration"])
                off = [float(el.attrib["OffPower"])] * int(el.attrib["OffDuration"])
                for _ in range(repeat):
                    out.extend(on)
                    out.extend(off)
            elif tag == "freeride":
                # No objective target profile, so this workout cannot be
                # completion-matched on shape. KNOWN GAP since sprints became
                # untargeted freeride blocks: a sprint session now falls back
                # to the duration/TSS match alone. Acceptable - a 12s maximal
                # effort was never trackable against an ERG target anyway -
                # but if sprints ever become plan-scheduled (see the goal
                # work) this wants revisiting, e.g. by matching only the
                # targeted blocks and ignoring the free ones.
                return []
        except (KeyError, TypeError, ValueError):
            return []
    return out


def _profile_evidence(activity: dict, workout: dict) -> Optional[tuple]:
    """Return profile match quality and FTP evidence when both streams exist."""
    target = np.asarray(_zwo_fraction_profile(
        workout.get("zwo") or workout.get("zwo_or_segments") or ""
    ), dtype=float)
    actual_raw = ((activity.get("streams") or {}).get("power") or [])
    actual = np.asarray(
        [0.0 if p is None else float(p) for p in actual_raw], dtype=float
    )
    if target.size < 600 or actual.size < 600:
        return None
    # Align by relative workout progress; this tolerates small recording/pause
    # differences while preserving the prescribed interval shape.
    x_old = np.linspace(0.0, 1.0, actual.size)
    x_new = np.linspace(0.0, 1.0, target.size)
    aligned = np.interp(x_new, x_old, actual)
    eligible = target >= 0.50
    if not eligible.any():
        return (0.0, None)
    scale = float(np.median(aligned[eligible] / target[eligible]))
    export_ftp = float(workout.get("export_ftp") or 0)
    if scale <= 0:
        return (0.0, None)
    if export_ftp and not 0.60 * export_ftp <= scale <= 1.40 * export_ftp:
        return (0.0, None)
    if not export_ftp and not 50.0 <= scale <= 600.0:
        return (0.0, None)
    expected = target[eligible] * scale
    mae = float(np.mean(np.abs(aligned[eligible] - expected) / scale))
    compliance = max(0.0, min(1.0, 1.0 - mae))
    hard_enough = int((target >= 0.85).sum()) >= PROFILE_MIN_HARD_SECONDS
    return compliance, scale if hard_enough else None


def _requires_power_profile(workout: dict) -> bool:
    """Whether a prescription has a usable objective target-power profile."""
    target = _zwo_fraction_profile(
        workout.get("zwo") or workout.get("zwo_or_segments") or ""
    )
    return len(target) >= 600 and any(power >= 0.50 for power in target)


def plan_workout_completion_verified(user_id: int, workout: dict) -> bool:
    """Whether a stored completion is strong enough to expose RPE feedback.

    Legacy prescriptions without an objective profile retain their historical
    duration/TSS completion semantics.  Profile-backed prescriptions require
    linked, user-owned activity data that still passes duration and objective
    profile checks. Old weak links remain stored but are treated as unverified
    rather than silently deleted.
    """
    if not workout or workout.get("completed_activity_id") is None:
        return False
    if not _requires_power_profile(workout):
        return True
    duration = float(workout.get("duration_s") or 0)
    activity = db.get_activity(user_id, workout["completed_activity_id"])
    if not activity or duration <= 0:
        return False
    try:
        activity_date = _dt.datetime.fromisoformat(
            str(activity.get("start_time") or "")
        ).date().isoformat()
    except (TypeError, ValueError):
        return False
    if activity_date != workout.get("date"):
        return False
    duration_error = abs(float(activity.get("duration_s") or 0) - duration) / duration
    if duration_error > STANDALONE_DURATION_TOLERANCE:
        return False
    evidence = _profile_evidence(activity, workout)
    return bool(evidence and evidence[0] >= PROFILE_MIN_COMPLIANCE)


def _match_standalone_completions(
    user_id: int, now: Optional[_dt.datetime] = None
) -> int:
    """Match persisted one-off exports using date, duration and target profile."""
    db.init_db()
    now = now or utc_now()
    used = db.completed_activity_ids(user_id)
    marked = 0
    for workout in db.incomplete_standalone_workouts_up_to(
        user_id, now.date().isoformat()
    ):
        best = None
        best_score = None
        for summary in db.activities_on_date(user_id, workout["scheduled_date"]):
            if summary["id"] in used:
                continue
            plan_duration = float(workout.get("duration_s") or 0)
            if plan_duration <= 0:
                continue
            duration_error = abs(float(summary.get("duration_s") or 0) - plan_duration) / plan_duration
            if duration_error > STANDALONE_DURATION_TOLERANCE:
                continue
            activity = db.get_activity(user_id, summary["id"])
            evidence = _profile_evidence(activity or {}, workout)
            if evidence is None:
                continue
            compliance, effective = evidence
            if compliance < PROFILE_MIN_COMPLIANCE:
                continue
            score = duration_error + (1.0 - compliance)
            if best_score is None or score < best_score:
                best = (summary, compliance, effective)
                best_score = score
        if best and db.mark_standalone_completed(
            user_id, workout["id"], best[0]["id"], workout["scheduled_date"],
            best[1], best[2],
        ):
            used.add(best[0]["id"])
            marked += 1
    return marked


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


def match_plan_workout_completion(
    user_id: int,
    workout_id: int,
    on_date: Optional[_dt.date] = None,
) -> bool:
    """Match one non-future plan workout against already-imported power data.

    This is the narrow calendar-click path: it never scans files, never looks
    at another user's data, and never considers another plan workout.  Unlike
    the legacy batch matcher, an objective power-profile match is required.
    """
    db.init_db()
    on_date = on_date or utc_today()
    workout = db.get_plan_workout(user_id, workout_id)
    if (
        not workout
        or workout.get("completed_activity_id") is not None
        or workout.get("date") != on_date.isoformat()
    ):
        return False

    used = db.completed_activity_ids(user_id)
    best = None
    best_score = None
    duration = float(workout.get("duration_s") or 0)
    if duration <= 0:
        return False

    for summary in db.activities_on_date(user_id, workout["date"]):
        if summary["id"] in used:
            continue
        duration_error = abs(float(summary.get("duration_s") or 0) - duration) / duration
        if duration_error > STANDALONE_DURATION_TOLERANCE:
            continue
        activity = db.get_activity(user_id, summary["id"])
        evidence = _profile_evidence(activity or {}, workout)
        if evidence is None:
            continue
        compliance, effective = evidence
        if compliance < PROFILE_MIN_COMPLIANCE:
            continue
        score = duration_error + (1.0 - compliance)
        if best_score is None or score < best_score:
            best = (summary, compliance, effective)
            best_score = score

    if best is None:
        return False
    return db.mark_plan_workout_completed(
        user_id,
        workout_id,
        best[0]["id"],
        workout["date"],
        best[1],
        best[2],
    )


def link_selected_plan_workout(
    user_id: int, workout_id: int, activity_id: int
) -> bool:
    """Authoritatively link a saved in-app ride to its selected plan workout.

    The explicit Ride selection is authoritative even when the ride happened
    on a different date or does not satisfy heuristic profile matching. Both
    records must belong to the same user, and an activity may only be consumed
    by one persisted workout.
    """
    db.init_db()
    workout = db.get_plan_workout(user_id, workout_id)
    activity = db.get_activity(user_id, activity_id)
    if (
        not workout
        or not activity
        or workout.get("completed_activity_id") is not None
        or activity_id in db.completed_activity_ids(user_id)
    ):
        return False
    try:
        completed_date = _dt.datetime.fromisoformat(
            str(activity.get("start_time") or "")
        ).date().isoformat()
    except (TypeError, ValueError):
        return False

    compliance = effective = None
    if completed_date == workout.get("date"):
        evidence = _profile_evidence(activity, workout)
        if evidence is not None and evidence[0] >= PROFILE_MIN_COMPLIANCE:
            compliance, effective = evidence
    return db.mark_plan_workout_completed(
        user_id,
        workout_id,
        activity_id,
        completed_date,
        compliance,
        effective,
    )


def manually_complete_plan_workout(user_id: int, workout_id: int) -> str:
    """Link the closest-duration eligible activity on the workout's date.

    Manual calendar completion deliberately does not require power-profile
    evidence, but it remains user/date scoped and never reuses an activity.
    """
    db.init_db()
    workout = db.get_plan_workout(user_id, workout_id)
    if not workout:
        return "not_found"
    if workout.get("completed_activity_id") is not None:
        return "already_completed"
    try:
        scheduled = _dt.date.fromisoformat(str(workout.get("date") or ""))
    except (TypeError, ValueError):
        return "invalid_date"
    if scheduled > utc_today():
        return "future"

    activities = db.activities_on_date(user_id, workout["date"])
    if not activities:
        return "no_activity"
    used = db.completed_activity_ids(user_id)
    eligible = [activity for activity in activities if activity["id"] not in used]
    if not eligible:
        return "activities_used"
    duration = float(workout.get("duration_s") or 0)
    best = min(
        eligible,
        key=lambda activity: (
            abs(float(activity.get("duration_s") or 0) - duration),
            str(activity.get("start_time") or ""),
            int(activity["id"]),
        ),
    )
    activity = db.get_activity(user_id, best["id"])
    evidence = _profile_evidence(activity or {}, workout)
    compliance = effective = None
    if evidence is not None and evidence[0] >= PROFILE_MIN_COMPLIANCE:
        compliance, effective = evidence
    if db.mark_plan_workout_completed(
        user_id,
        workout_id,
        best["id"],
        workout["date"],
        compliance,
        effective,
    ):
        return "completed"
    return "conflict"


def match_plan_completions(user_id: int, now: Optional[_dt.datetime] = None) -> int:
    """Mark plan workouts completed by matching same-day activities.

    For every not-yet-completed plan workout dated today or earlier, find the
    user's best-matching activity on that date (each activity completes at most
    one workout). Returns the number of workouts newly marked completed.
    """
    db.init_db()
    now = now or utc_now()
    # Scheduled plan commitments always get first refusal on same-day rides.
    marked = 0
    used = db.completed_activity_ids(user_id)
    for workout in db.incomplete_plan_workouts_up_to(user_id, now.date().isoformat()):
        best = None
        best_score = None
        for act in db.activities_on_date(user_id, workout["date"]):
            if act["id"] in used:
                continue
            full = db.get_activity(user_id, act["id"])
            evidence = _profile_evidence(full or {}, workout)
            duration = float(workout.get("duration_s") or 0)
            duration_error = (
                abs(float(act.get("duration_s") or 0) - duration) / duration
                if duration > 0 else 999.0
            )
            if _requires_power_profile(workout):
                if (
                    evidence is not None
                    and duration_error <= STANDALONE_DURATION_TOLERANCE
                ):
                    compliance, effective = evidence
                    score = (
                        duration_error + (1.0 - compliance)
                        if compliance >= PROFILE_MIN_COMPLIANCE
                        else None
                    )
                else:
                    compliance = effective = None
                    score = None
            else:
                compliance = effective = None
                fallback = _completion_score(act, workout)
                score = 1.0 + fallback if fallback is not None else None
            if score is not None and (best_score is None or score < best_score):
                best, best_score = (act, compliance, effective), score
        if best is not None:
            if db.mark_plan_workout_completed(
                user_id, workout["id"], best[0]["id"], workout["date"],
                best[1], best[2],
            ):
                used.add(best[0]["id"])
                marked += 1
    return marked + _match_standalone_completions(user_id, now)


def match_standalone_completions(
    user_id: int, now: Optional[_dt.datetime] = None
) -> int:
    """Run completion matching with scheduled plans receiving first refusal."""
    return match_plan_completions(user_id, now)


def _neutral_rpe(workout_type: str) -> int:
    return 9 if workout_type == "vo2max" else 8


def apply_rpe_ftp_feedback(
    user_id: int, now: Optional[_dt.datetime] = None
) -> Optional[float]:
    """Apply one bounded FTP step from multiple new compliant hard workouts.

    Expected effort is type-aware: VO2 RPE 9 is neutral, while threshold and
    sweet-spot RPE 8 are neutral. Evidence is consumed in reversible batches.

    The step size is driven by how far RPE sits from neutral, not by the
    workout's own realized wattage: an ERG-controlled ride tracks its target
    power almost exactly, so "effective_ftp" (actual/target) mostly just
    reflects the FTP the workout was generated from and can't reveal that the
    prescribed intensity felt too easy - which is exactly the case a returning
    rider needs this to catch. Wattage evidence still applies (via max()) so a
    genuinely higher realized effort (e.g. non-ERG, free ride) isn't ignored.
    """
    now = now or utc_now()
    settings = db.get_user_settings(user_id)
    if settings.get("ftp") and float(settings["ftp"]) > 0:
        return None
    latest = db.latest_ftp(user_id)
    if not latest or latest.get("source") != "estimated":
        return None
    since = (now.date() - _dt.timedelta(days=FTP_FEEDBACK_WINDOW_DAYS)).isoformat()
    evidence = []
    for item in db.unused_feedback_evidence(user_id, since):
        if float(item.get("compliance") or 0) < PROFILE_MIN_COMPLIANCE:
            continue
        # Plan completions must still verify (linked activity, matching date,
        # duration and objective profile) before their RPE can move FTP; a stale
        # or weak completion link is treated as unverified rather than trusted.
        if item.get("kind") == "plan":
            workout = db.get_plan_workout(user_id, int(item["id"]))
            if not plan_workout_completion_verified(user_id, workout or {}):
                continue
        evidence.append(item)
    low = [
        e for e in evidence
        if int(e["rpe"]) <= (8 if e["type"] == "vo2max" else 7)
    ]
    high = [
        e for e in evidence
        if int(e["rpe"]) >= (10 if e["type"] == "vo2max" else 9)
    ]
    current = float(latest["ftp_watts"])
    chosen: List[dict]
    if len(low) >= FTP_FEEDBACK_MIN_WORKOUTS:
        chosen = low
        demonstrated = float(np.median([e["effective_ftp"] for e in chosen]))
        rpe_gap = float(np.median(
            [_neutral_rpe(e["type"]) - int(e["rpe"]) for e in chosen]
        ))
        rpe_implied = current * (1.0 + FTP_RPE_STEP_PER_POINT * max(0.0, rpe_gap))
        desired = max(current, demonstrated, rpe_implied)
    elif len(high) >= FTP_FEEDBACK_MIN_WORKOUTS:
        chosen = high
        rpe_gap = float(np.median(
            [int(e["rpe"]) - _neutral_rpe(e["type"]) for e in chosen]
        ))
        desired = current * (1.0 - FTP_RPE_STEP_PER_POINT * max(0.0, rpe_gap))
    else:
        return None
    limit = current * FTP_FEEDBACK_MAX_STEP
    updated = max(50.0, min(600.0, current + max(-limit, min(limit, desired - current))))
    updated = round(updated, 1)
    delta = round(updated - current, 1)
    if abs(updated - current) < 0.1:
        batch = db.apply_feedback_batch(
            user_id, latest["date"], current, 0.0, chosen
        )
        return current if batch is not None else None
    batch = db.apply_feedback_batch(
        user_id, latest["date"], updated, delta, chosen
    )
    return updated if batch is not None else None


def save_workout_rpe(
    user_id: int, kind: str, workout_id: int, rpe: int,
    now: Optional[_dt.datetime] = None,
) -> bool:
    """Save/correct a rating, reversing its prior feedback batch first."""
    if kind not in ("plan", "standalone"):
        return False
    if kind == "plan":
        workout = db.get_plan_workout(user_id, workout_id)
        if not plan_workout_completion_verified(user_id, workout or {}):
            return False
    db.rollback_feedback_for_workout(user_id, kind, workout_id)
    if kind == "plan":
        saved = db.set_plan_workout_rpe(user_id, workout_id, rpe)
    else:
        saved = db.set_standalone_rpe(user_id, workout_id, rpe)
    if saved:
        apply_rpe_ftp_feedback(user_id, now)
    return saved


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
