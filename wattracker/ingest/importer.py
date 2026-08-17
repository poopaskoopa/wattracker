"""Scan and import .fit activities into the database (idempotent, per-user)."""
from __future__ import annotations

import bisect
import datetime as _dt
import hashlib
import logging
import os
import tempfile
import time
import xml.etree.ElementTree as _ET
from typing import Callable, Dict, List, Optional

import numpy as np

from .. import db
from ..backend import get_backend
from ..config import db_path
from ..ftp_provenance import is_asserted_source
from ..ftp_rescore import rescore_imported_activities
from ..metrics.power import (
    DEFAULT_FTP,
    FTP_ASSERTION_MAX_WATTS,
    FTP_ASSERTION_MIN_WATTS,
    FTP_PLAUSIBLE_MIN_WATTS,
    asserted_ftp,
    estimate_ftp,
    intensity_factor,
    is_plausible_ftp,
    normalized_power,
    training_stress_score,
)
from ..metrics import profile_store
from ..prescribe.plan import HARD_STEADY_POWER
from ..timeutil import parse_naive, utc_now, utc_today
from .fit_parser import parse_fit

log = logging.getLogger(__name__)


_log = logging.getLogger(__name__)

# DEFAULT_FTP is re-exported from metrics.power, which owns the one no-data
# placeholder in the app (issue #55). It is a stated placeholder, not a
# measurement, and is resolved at read time by current_ftp - never written to
# ftp_history.


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
# How far the fitted power scale may sit from the FTP the workout was exported
# at before the ride stops being a plausible completion of it. This is a
# plausibility rail, NOT the thing that separates a real completion from a
# look-alike: the fit has a free scale factor, so the false-positive band and
# the genuine band overlap almost exactly (both run from ~0.86x to 1.30x of the
# export FTP). Structure does the separating - see PROFILE_MIN_STRUCTURE_RATIO.
# Kept wide on purpose so a rider whose trainer/Zwift FTP is set well away from
# the estimate we exported at is never rejected on wattage alone.
PROFILE_SCALE_MIN_FRACTION = 0.60
PROFILE_SCALE_MAX_FRACTION = 1.40
# Shape compliance is scale-free and is a plain mean-absolute-error, so it only
# ever asks "was the rider roughly the right average multiple of the target?".
# A right-sized threshold hour is mostly one long near-steady block, so a Zone 2
# endurance hour scores 0.92 against it (the target's own mean deviation from
# its median is only ~0.08 FTP, so "nowhere near the recoveries" is a small
# error) and is silently recorded as a completed threshold session, feeding a
# fabricated effective_ftp into the RPE feedback loop.
#
# So compliance is paired with a structure check: the ride has to show that it
# went hard where the prescription asked for work and easy where it asked for
# recovery. That is the WORK/RECOVERY CONTRAST below.
#
# The obvious cheaper check - "does the ride vary as much as the prescription?",
# i.e. a whole-session dispersion ratio - does not survive short sessions. At 30
# minutes a third of the prescription is its 600 s warmup ramp, so the
# prescription's own dispersion is dominated by the ramp, and any ride with a
# normal warmup and cooldown reproduces that dispersion without ever doing an
# interval: measured look-alike ceilings of 0.81 at 30 min and 0.75 at 45 min
# against a genuine floor of 0.87, i.e. no usable gap. Whatever single constant
# is chosen, the warmup's share of total dispersion is duration-dependent, so
# the check cannot be made duration-robust by retuning it. Dispersion is kept
# only as the fallback for prescriptions with no identifiable work block (see
# _work_recovery_masks).
PROFILE_SMOOTH_WINDOW_S = 60
# Below this the prescription is itself near-flat (Zone 2 endurance, recovery)
# and has no structure to demand; the check stands down rather than rejecting a
# legitimately steady ride. Structured kinds sit far above it: threshold and
# sweet spot ~0.15, VO2max ~0.25, tempo ~0.12, versus endurance <=0.09.
PROFILE_STRUCTURED_DISPERSION = 0.10
# Fallback dispersion ratio, used only when no work block can be identified.
PROFILE_MIN_STRUCTURE_RATIO = 0.75

# ------------------------------------------------ work/recovery contrast
# The prescription's work seconds and its recovery seconds are known, so ask
# the ride the one question that actually separates the sessions: was the rider
# meaningfully harder during the prescribed work than during the prescribed
# recoveries, by as large a factor as was prescribed?
#
#   contrast = mean(ride over prescribed work seconds)
#              / mean(ride over prescribed recovery seconds)
#   ratio    = ride contrast / prescription contrast
#
# It is scale-free (a pure ratio), it never looks at the warmup or the cooldown
# (both fall outside the work span), and it is duration-robust because it
# compares like with like rather than integrating over the whole session. Any
# steady ride - flat, warmup+steady+cooldown, undulating, negative-split, or
# Zone 2 with surges - has roughly the same mean in both windows and lands near
# 1/prescribed_contrast, far below a real session.
#
# Measured over durations {30,45,60,75,90,120} x {threshold, sweet_spot,
# vo2max} x every variant x export FTP {150,200,250,300}, on rides that already
# pass the scale rail and the 0.90 compliance gate (11.8k look-alikes, 51.3k
# genuine completions):
#   look-alike ceiling  0.658 0.649 0.642 0.609 0.633 0.644
#   genuine floor       0.755 0.766 0.805 0.802 0.802 0.802
# 0.70 sits in that gap at every duration. The genuine floor is the pathology
# (a rider who abandoned the last interval, or a 2-minute stop recorded as a
# gap); ERG-perfect, lagged, jittery and paused rides sit at 0.9-1.1.
PROFILE_MIN_CONTRAST_RATIO = 0.70
# A power level must hold for this long to count as "the work" - short enough
# to catch 30/30s VO2max blocks, long enough that a warmup ramp passing through
# a level is never mistaken for one.
PROFILE_WORK_LEVEL_SECONDS = 240
# Both windows need this much time before the contrast means anything.
PROFILE_CONTRAST_MIN_SECONDS = 120
# The leading transition of every block is dropped (capped at a third of the
# block) so a trainer that takes time to settle is not read as non-compliance.
# 60 s covers ERG transitions out to tau = 60 s.
PROFILE_CONTRAST_ERODE_S = 60
# The ride is aligned by stretching it onto workout progress, so a pause or a
# dropout shifts it against the prescription. The best contrast within this
# much slack is taken; a look-alike gains nothing from shifting, having no
# blocks to line up.
PROFILE_CONTRAST_SHIFT_S = 120
PROFILE_CONTRAST_SHIFT_STEP_S = 15
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


def _asserted_override(user_id: int, settings: Optional[Dict] = None):
    """The rider's stated FTP from settings, as an ``AssertedFTP``, or None.

    ``user_settings.ftp`` is by definition an assertion - it is only ever
    written when a human types a number in. It is still bounded: /settings
    validates nothing today (issue #64), so a mistyped 2500 or a 0.64 planted
    by a bad migration must not become a scoring basis.
    """
    if settings is None:
        settings = db.get_user_settings(user_id)
    stated = settings.get("ftp")
    override = asserted_ftp(stated)
    if override is not None:
        return override
    try:
        number = float(stated)
    except (TypeError, ValueError, OverflowError):
        number = 0.0
    if number > 0:
        _log.warning(
            "ignoring FTP override for user %s: %.3f W is outside the "
            "%.0f-%.0f W range an FTP can physically take",
            user_id, number, FTP_ASSERTION_MIN_WATTS, FTP_ASSERTION_MAX_WATTS,
        )
    return None


def current_ftp(
    user_id: int,
    now: Optional[_dt.datetime] = None,
    extra_power: Optional[List[List[float]]] = None,
) -> float:
    """Resolve the current FTP for a user.

    Precedence: user's FTP override -> latest ftp_history value -> fresh
    detraining-decayed estimate anchored at `now`. Falls back to DEFAULT_FTP
    when nothing usable is available.

    Every ESTIMATED basis must clear ``is_plausible_ftp`` before it is returned.
    The estimator is anchored at wall-clock ``now``, so a rider whose newest
    ride in the database is years old gets an estimate decayed across that whole
    gap - a number that is honest about "what could they do today" but is
    physically absurd as a wattage (issue #60 saw 0.64 W). Such a value is not a
    weak measurement, it is a failed one, and it is discarded rather than
    returned.

    A rider's own ASSERTION - a manual override, or an ``ftp_history`` row whose
    source says the rider entered it - is not subject to the estimate floor,
    however low: that is their statement, not our estimate. It is returned as an
    ``AssertedFTP`` so the rest of this call chain need not re-derive that; the
    durable record of the same fact is in the database (see
    ``wattracker.ftp_provenance``), so a rescorer that reads a basis back out of
    SQLite reaches the same verdict. An assertion outside the human range is
    still refused - see ``asserted_ftp``.
    """
    db.init_db()
    settings = db.get_user_settings(user_id)
    override = _asserted_override(user_id, settings)
    if override is not None:
        return override
    latest = db.latest_ftp(user_id)
    if latest and latest.get("ftp_watts", 0) > 0:
        stored = float(latest["ftp_watts"])
        if is_asserted_source(latest.get("source")):
            asserted = asserted_ftp(stored)
            if asserted is not None:
                return asserted
            _log.warning(
                "ignoring asserted FTP history row for user %s: %.3f W is "
                "outside the %.0f-%.0f W range an FTP can physically take",
                user_id, stored, FTP_ASSERTION_MIN_WATTS, FTP_ASSERTION_MAX_WATTS,
            )
        elif is_plausible_ftp(stored):
            return stored
        else:
            _log.warning(
                "ignoring implausible stored FTP estimate for user %s: %.3f W "
                "(floor %.0f W)", user_id, stored, FTP_PLAUSIBLE_MIN_WATTS
            )
    ftp = _current_estimate(db.full_activities(user_id), now, extra_power)
    if is_plausible_ftp(ftp):
        return ftp
    if ftp > 0:
        _log.warning(
            "discarding implausible FTP estimate for user %s: %.3f W "
            "(floor %.0f W); using default %.0f W",
            user_id, ftp, FTP_PLAUSIBLE_MIN_WATTS, DEFAULT_FTP,
        )
    return DEFAULT_FTP


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


def _record_asserted_ftp(
    user_id: int, watts: float, now: _dt.datetime
) -> bool:
    """Make ``ftp_history`` say what the rider asserted, for today.

    Written with source='manual', which is what makes the provenance durable:
    any reader can then tell this basis from an estimate (see
    ``wattracker.ftp_provenance``). ``add_ftp_entry`` replaces today's row, so
    an estimate recorded earlier today stops contradicting the assertion.
    """
    today = now.date().isoformat()
    latest = db.latest_ftp(user_id)
    if (
        latest
        and latest.get("date") == today
        and is_asserted_source(latest.get("source"))
        and abs(float(latest.get("ftp_watts") or 0.0) - watts) < 0.05
    ):
        return False
    db.add_ftp_entry(user_id, today, watts, "manual")
    return True


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

    While the rider holds an FTP OVERRIDE, their assertion is recorded instead
    of the estimate. ``current_ftp`` already ignores the estimate in that case,
    so writing it to history publishes a second, contradicting record of "this
    rider's FTP" that every other reader of the database will pick up - and one
    of them, the offline rescore in #59, re-scores the rider's rides from it.
    A rider who states 40 W and watches the importer score their rides at IF 5
    must not have that silently rebased to a 182 W estimate by a background
    pass. The assertion is what the app is using; the assertion is what history
    records.
    """
    db.init_db()
    now = now or utc_now()
    override = _asserted_override(user_id)
    if override is not None:
        return _record_asserted_ftp(user_id, float(override), now)
    est = _current_estimate(db.full_activities(user_id), now)
    if est <= 0:
        return False
    if not is_plausible_ftp(est):
        # A failed estimate must never enter FTP history: current_ftp reads the
        # latest row back as an authoritative basis, so persisting one turns a
        # single bad evaluation into the scoring basis for every subsequent
        # import (issue #60 - this is exactly how 0.6-32 W FTPs reached 2,199
        # activity rows).
        _log.warning(
            "refusing to record implausible FTP estimate for user %s: %.3f W "
            "(floor %.0f W)", user_id, est, FTP_PLAUSIBLE_MIN_WATTS
        )
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
    """Build the stored activity row, scoring it against ``ftp``.

    The ride is scored only against a plausible FTP. NP and average power are
    measurements and are stored regardless, but IF and TSS are quotients of the
    FTP basis and are meaningless when it is not a real wattage - so an
    implausible basis leaves them at 0 rather than storing a number that is
    wrong by orders of magnitude. A row with ``np > 0`` and ``if_ == 0`` is
    therefore identifiable afterwards as "never scored", which is the state
    issue #62's repair pass needs to find. This is the last rail on the import
    path: it holds even for callers that resolve the FTP themselves and pass it
    in. (The rail of last resort is in ``intensity_factor`` /
    ``training_stress_score`` themselves, which no scorer can bypass.)

    "Plausible" is a test on the basis, not merely on the number: an FTP the
    rider asserted arrives here as an ``AssertedFTP`` and is honoured however
    low, because refusing it would silently store zero training load for a rider
    who told us exactly what their FTP is. Only our own estimates are filtered.
    """
    streams = parsed["streams"]
    power = streams.get("power") or []
    hr = streams.get("heartrate") or []
    distance = streams.get("distance") or []
    duration_s = parsed["duration_s"]

    npw = normalized_power(power) if power else 0.0
    scorable = is_plausible_ftp(ftp)
    if not scorable and float(ftp or 0.0) > 0:
        _log.warning(
            "not scoring %s: FTP basis %.3f W is below the %.0f W plausibility "
            "floor; IF/TSS left unset", filename, float(ftp), FTP_PLAUSIBLE_MIN_WATTS
        )
    ifv = intensity_factor(npw, ftp) if scorable else 0.0
    tss = training_stress_score(duration_s, npw, ftp) if scorable else 0.0
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


def ingest_file(
    user_id: int,
    path: str,
    ftp: Optional[float] = None,
    filename: Optional[str] = None,
    ensure_curve: bool = True,
) -> Optional[int]:
    """Parse and store a single .fit file for a user. Returns id or None if dup.

    ``filename`` overrides the name recorded on the activity row. It matters
    whenever ``path`` is a temporary copy rather than the file the rider knows
    - an upload, or a .fit fetched from a connector - because that column is
    what tells an imported ride from an in-app one (``is_in_app_activity`` /
    ``db.IN_APP_FILENAME_SQL``), and cross-source duplicate linking reads it.
    """
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
    record = _build_record(parsed, filename or os.path.basename(path), ftp)
    new_id = db.insert_activity(user_id, record)
    if new_id is not None:
        # The rider may have recorded this same ride in-app at the same time.
        link_duplicate_activity(user_id, new_id)
        if ensure_curve:
            from ..metrics import curve_store
            curve_store.ensure(user_id)
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
    if linked:
        from ..metrics import curve_store
        curve_store.ensure(user_id)
    return linked


def _user_activities_dir(user_id: int, backend=None) -> Optional[str]:
    """The folder this user's scans read from (stored override, else discovery).

    The stored value is re-confined on every read rather than trusted because
    it is in the database: a row can predate the write-side check, or arrive
    from a restored backup or a hand-edited DB. A stored value that escapes
    means NOTHING is scanned for this user (None) - deliberately not "fall back
    to OS discovery", which would quietly import from a folder the user never
    configured.

    The confinement goes through the backend because only it knows whose
    machine the path is on: in a server/client install these are the client's
    folders and the connector applies the check locally.
    """
    backend = backend or get_backend(user_id)
    override = db.get_user_settings(user_id).get("activities_dir")
    if override:
        return backend.confine_stored_dir(override)
    return backend.default_activities_dir()


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
    can surface live status: once with ``{"total": N}`` after listing, then
    after each file with ``processed``/``imported``/``skipped`` counts.

    The files themselves come from the backend, so in a server/client install
    this scans the *connector's* Zwift folder. The ``scanned_files`` cache is
    keyed by the path as that machine sees it, which is what keeps an
    incremental rescan cheap across the network - an unchanged file is never
    transferred, let alone parsed.
    """
    db.init_db()
    backend = get_backend(user_id)
    directory = directory or _user_activities_dir(user_id, backend)
    imported = 0

    def _report(**fields):
        if progress:
            progress(fields)

    listing = backend.list_activities(directory)
    # The backend resolves the folder in remote mode, so believe it over the
    # value we asked for.
    directory = listing.directory
    if not listing.exists:
        _report(total=0, processed=0, imported=0, skipped=0, not_offered=0)
        return {"found": 0, "imported": 0, "skipped": 0, "not_offered": 0,
                "completed": 0, "directory": directory, "exists": False}

    ftp = current_ftp(user_id)
    imported_activity_ids: List[int] = []

    # Files the backend declined to offer (Zwift's in-progress buffer, one that
    # vanished mid-listing, a .fit that resolves out of the folder) still count
    # as found-and-skipped. They are also reported on their own, because they
    # are not duplicates and saying so is the difference between a rider being
    # told to look at their connector's log and being told nothing at all.
    found = listing.skipped
    skipped = listing.skipped
    _report(total=len(listing.files) + listing.skipped, processed=found,
            imported=0, skipped=skipped, not_offered=listing.skipped)

    seen = db.seen_files(user_id)
    for entry in listing.files:
        found += 1
        prev = seen.get(entry.path)
        if prev is not None and prev[0] == entry.mtime and prev[1] == entry.size:
            # Already scanned, unchanged - skip without parsing (or, in remote
            # mode, without even transferring it).
            skipped += 1
            _report(processed=found, imported=imported, skipped=skipped)
            continue

        try:
            with backend.readable_activity(entry.path) as local_path:
                new_id = ingest_file(
                    user_id, local_path, ftp=ftp, filename=entry.name,
                    ensure_curve=False,
                )
        except Exception:
            skipped += 1
            _report(processed=found, imported=imported, skipped=skipped)
            continue

        # Record whether it was a new import or a duplicate, so subsequent
        # rescans skip it without parsing. Keyed by the path as the owning
        # machine sees it.
        db.record_scanned_file(user_id, entry.path, entry.mtime, entry.size)
        if new_id is None:
            skipped += 1
        else:
            imported += 1
            imported_activity_ids.append(new_id)
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
        rescore_imported_activities(
            user_id, imported_activity_ids, path=db_path()
        )
        completed = match_plan_completions(user_id)
        # New rides move the rider's measured capacities (a new 5s or 5min
        # peak, a new FTP), and every prescription is built on the STORED
        # snapshot - so refresh it here, after FTP re-evaluation, rather than
        # leaving the plan quoting yesterday's rider until the nightly sweep.
        # Deliberately inside the `imported > 0` guard: nothing new landed
        # means nothing derived changed.
        profile_store.refresh(user_id)
        from ..metrics import curve_store
        curve_store.ensure(user_id)

    return {
        "found": found,
        "imported": imported,
        "skipped": skipped,
        # The subset of `skipped` the owning machine declined to offer, so the
        # UI can stop calling a symlinked .fit a duplicate.
        "not_offered": listing.skipped,
        "completed": completed,
        "directory": directory,
        # Reported by whoever owns the folder. The server cannot answer this
        # itself in a server/client install - the path is on another machine,
        # so an os.path.isdir here would say "no" to every valid folder.
        "exists": True,
    }


def save_ride_record(
    user_id: int,
    started_at: _dt.datetime,
    duration_s: int,
    samples: Dict[str, list],
    session_name: str,
    ftp: float,
    workout_id: Optional[int] = None,
) -> "tuple[Optional[int], dict]":
    """Store a ride recorded in-app as an activity. Returns (activity_id, record).

    One code path for both ways a ride can reach here: the controller running
    in this process, and a connector uploading a ride it buffered while the
    network was down. They must produce byte-identical rows - a ride that
    happened to span a reconnect is still one ride - so neither gets its own
    copy of this chain.

    ``started_at`` is naive UTC, like the timestamps parsed out of .fit files:
    the same ride recorded by both the app and Zwift has to land on the same
    instant, not four hours apart.
    """
    from ..metrics import profile_store

    n = len(samples.get("power") or [])
    streams = {
        "time": [
            (started_at + _dt.timedelta(seconds=i)).isoformat() for i in range(n)
        ],
        "power": samples.get("power") or [],
        "cadence": samples.get("cadence") or [],
        "heartrate": samples.get("heartrate") or [],
        "distance": [],
        "altitude": [],
    }
    parsed = {
        "start_time": started_at.isoformat(),
        "duration_s": int(duration_s),
        "streams": streams,
    }
    # The app is UTC end to end, so the ride is named by its UTC date - a
    # late-evening ride west of Greenwich reads as the next day.
    name = f"Ride {started_at.date().isoformat()} {session_name}"
    record = _build_record(parsed, name, ftp)
    activity_id = db.insert_activity(user_id, record)
    if activity_id is not None and workout_id is not None:
        link_selected_plan_workout(user_id, workout_id, activity_id)
    if activity_id is not None:
        link_duplicate_activity(user_id, activity_id)
        from ..metrics import curve_store
        curve_store.ensure(user_id)
    try:
        maybe_update_ftp(user_id)
    except Exception:
        pass
    if activity_id is not None:
        try:
            profile_store.refresh(user_id)
        except Exception:
            log.warning(
                "rider profile refresh after in-app ride failed", exc_info=True
            )
    return activity_id, record


def ingest_upload(
    user_id: int,
    filename: str,
    content: bytes,
    *,
    ftp: Optional[float] = None,
    refresh: bool = True,
) -> Optional[int]:
    """Ingest an uploaded .fit file (raw bytes) for a user.

    ``refresh=False`` is used by callers that ingest a batch and perform the
    derived-state refresh once after all files have landed.
    """
    base = os.path.basename(filename or "upload.fit")
    if not base.lower().endswith(".fit"):
        # This name is now recorded on the activity row (it used to be the temp
        # file's), and db.IN_APP_FILENAME_SQL classifies on that column: a
        # 'Ride <date> ...' name WITHOUT a .fit extension means the ride was
        # recorded in-app. An upload is not, and the rider chooses this string,
        # so it must not be able to claim that shape.
        #
        # The invariant is "ends in .fit", not "has an extension". Requiring
        # only an extension left the same hole open one character along:
        # 'Ride 2026-01-01 x.gpx' and 'Ride 2026-01-01 x.' both have one, both
        # were stored verbatim, and both are classified in-app by
        # db.IN_APP_FILENAME_SQL and importer.is_in_app_activity alike - the
        # two classifiers agreed with each other and were both wrong.
        base += ".fit"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".fit")
    try:
        tmp.write(content)
        tmp.flush()
        tmp.close()
        # Record the name the rider uploaded, not the temp file's - see
        # ingest_file's ``filename`` argument.
        result = ingest_file(
            user_id, tmp.name, ftp=ftp, filename=base, ensure_curve=refresh
        )
        if refresh:
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


def _smoothed(values: np.ndarray, window: int = PROFILE_SMOOTH_WINDOW_S) -> np.ndarray:
    """Boxcar-smoothed copy, edges dropped so no artificial flattening creeps in."""
    if values.size <= window:
        return values
    return np.convolve(values, np.ones(window) / window, mode="valid")


def _erode_blocks(mask: np.ndarray, seconds: int) -> np.ndarray:
    """Drop the leading transition of every contiguous run in `mask`.

    Capped at a third of the run so short blocks (30/30s VO2max) survive.
    """
    out = mask.copy()
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return out
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = [idx[0]] + [idx[i + 1] for i in breaks]
    ends = [idx[i] for i in breaks] + [idx[-1]]
    for start, end in zip(starts, ends):
        out[start:start + min(seconds, (end - start + 1) // 3)] = False
    return out


def _work_level(target: np.ndarray) -> Optional[float]:
    """The power level of the prescription's main work block, or None.

    The highest level the prescription holds for PROFILE_WORK_LEVEL_SECONDS.
    "Highest sustained" rather than "maximum" so a handful of sprint or surge
    seconds never becomes the definition of the work.
    """
    levels, seconds = np.unique(np.round(target, 2), return_counts=True)
    sustained = levels[(seconds >= PROFILE_WORK_LEVEL_SECONDS) & (levels >= 0.60)]
    return float(sustained.max()) if sustained.size else None


def _work_recovery_masks(target: np.ndarray) -> Optional[tuple]:
    """Second masks for the prescribed work and the prescribed recoveries.

    Both are confined to the work span - from the first work second to the
    last - so the warmup ramp and the cooldown are excluded from the contrast
    entirely. Returns None when the prescription has no identifiable work block
    or too little of either window to compare.
    """
    level = _work_level(target)
    if level is None:
        return None
    work_floor = 0.95 * level
    hot = np.flatnonzero(target >= work_floor)
    if hot.size == 0:
        return None
    span = np.zeros(target.size, dtype=bool)
    span[hot[0]:hot[-1] + 1] = True
    work = _erode_blocks(span & (target >= work_floor), PROFILE_CONTRAST_ERODE_S)
    easy = _erode_blocks(span & (target <= 0.80 * work_floor),
                         PROFILE_CONTRAST_ERODE_S)
    if (work.sum() < PROFILE_CONTRAST_MIN_SECONDS
            or easy.sum() < PROFILE_CONTRAST_MIN_SECONDS):
        return None
    return work, easy


def _contrast(curve: np.ndarray, work: np.ndarray,
              easy: np.ndarray) -> Optional[float]:
    """How much harder `curve` is over the work seconds than the easy ones."""
    recovery = float(curve[easy].mean())
    if recovery <= 1e-6:
        return None
    return float(curve[work].mean()) / recovery


def _best_contrast(curve: np.ndarray, work: np.ndarray,
                   easy: np.ndarray) -> Optional[float]:
    """Best contrast over PROFILE_CONTRAST_SHIFT_S of alignment slack."""
    best = None
    for shift in range(-PROFILE_CONTRAST_SHIFT_S, PROFILE_CONTRAST_SHIFT_S + 1,
                       PROFILE_CONTRAST_SHIFT_STEP_S):
        value = _contrast(np.roll(curve, -shift), work, easy)
        if value is not None and (best is None or value > best):
            best = value
    return best


def _structure_ok(aligned_fractions: np.ndarray, target: np.ndarray) -> bool:
    """Whether the ride did the session that was prescribed, not just its average.

    Both curves are in FTP fractions (the activity divided by its fitted scale)
    and share a length, so they are directly comparable. See
    PROFILE_MIN_CONTRAST_RATIO for why contrast rather than dispersion.
    """
    prescribed_spread = float(_smoothed(target).std())
    if prescribed_spread < PROFILE_STRUCTURED_DISPERSION:
        return True  # a near-flat prescription demands no structure
    masks = _work_recovery_masks(target)
    if masks is None:
        # No identifiable work block: fall back to comparing dispersion.
        realized = float(_smoothed(aligned_fractions).std())
        return realized >= PROFILE_MIN_STRUCTURE_RATIO * prescribed_spread
    work, easy = masks
    prescribed = _contrast(target, work, easy)
    realized = _best_contrast(aligned_fractions, work, easy)
    if prescribed is None or prescribed <= 0:
        return True
    if realized is None:
        return False
    return realized >= PROFILE_MIN_CONTRAST_RATIO * prescribed


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
    if export_ftp and not (
        PROFILE_SCALE_MIN_FRACTION * export_ftp
        <= scale
        <= PROFILE_SCALE_MAX_FRACTION * export_ftp
    ):
        return (0.0, None)
    if not export_ftp and not 50.0 <= scale <= 600.0:
        return (0.0, None)
    if not _structure_ok(aligned / scale, target):
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


# Time in zone a full session of each type is built to deliver, in seconds.
# These are the planner's own 60-minute prescriptions (plan.hard_seconds of
# planner.build_workout(type, 60)) after the recent right-sizing, and they sit
# where the sports-science norms do: 36 min of threshold work (3x12), 24 min of
# sweet spot, 20 min of VO2max (the classic 4x4/5x4 band of 20-24 min). They
# are the yardstick a rated session's own time in zone is measured against, so
# a session that delivered a full dose keeps today's neutral RPE exactly.
FTP_REFERENCE_HARD_SECONDS = {
    "threshold": 2160,
    "sweet_spot": 1440,
    "vo2max": 1200,
}
# How far short of the reference dose can pull the neutral. Below RPE 5 a hard
# session is no longer meaningfully hard and the rating stops discriminating,
# so a very truncated session's expectation floors out rather than sliding
# toward 1 - which would turn any ordinary rating into "too hard, drop FTP".
NEUTRAL_RPE_MAX_DISCOUNT = 3


def _zwo_hard_seconds(zwo_str: Optional[str]) -> int:
    """Prescribed time in zone in a stored .zwo, in seconds.

    The same definition as ``plan.hard_seconds`` - intervals contribute their
    on-time, steady blocks count at or above ``HARD_STEADY_POWER`` - applied to
    the storage form. ``hard_seconds`` itself takes a planner ``Session``,
    which a completed workout no longer has: the database keeps the .zwo XML it
    exported, not the object graph it was built from. Rebuilding a Session from
    XML to call it would be more guesswork than reading the two element shapes
    that matter, so this reads the XML and imports the same threshold constant.

    FreeRide is deliberately not counted: the .zwo form carries no power at all
    (see ``zwo.session_to_zwo``), so its load fraction - the thing
    ``hard_seconds`` tests - is unrecoverable. It costs nothing here, because
    ``_zwo_fraction_profile`` refuses to build a target profile for a workout
    containing one, so such a workout can never be completion-matched and never
    becomes RPE evidence.
    """
    try:
        root = _ET.fromstring(zwo_str or "")
    except (_ET.ParseError, TypeError):
        return 0
    workout = root.find("workout")
    if workout is None:
        return 0
    total = 0
    for el in workout:
        tag = el.tag.lower()
        try:
            if tag == "intervalst":
                total += int(el.attrib.get("Repeat", 0)) * int(
                    el.attrib.get("OnDuration", 0)
                )
            elif tag == "steadystate":
                if float(el.attrib.get("Power", 0)) >= HARD_STEADY_POWER:
                    total += int(el.attrib.get("Duration", 0))
        except (TypeError, ValueError):
            return 0
    return total


def _neutral_rpe(workout_type: str, hard_s: Optional[int] = None) -> int:
    """The RPE a session of this type and this dose is expected to feel like.

    Type alone is not enough. RPE 8 is neutral for a threshold session that
    delivers its full 36 minutes in zone; the same 8 out of a 2x13 truncated to
    26 minutes would be a much harder session than was prescribed. Scaling the
    expectation with the delivered dose is what stops a truncated session's
    honest low rating from reading as "FTP is too low" - the rider did less
    work, so less fatigue is exactly what should have happened.

    Only shortfalls move the number. Extra time in zone is a different
    stimulus, not proof that a rating one point under type-neutral means the
    FTP is low, so the result is never above the type's neutral.
    """
    base = 9 if workout_type == "vo2max" else 8
    reference = FTP_REFERENCE_HARD_SECONDS.get(workout_type)
    if not reference or not hard_s or hard_s <= 0:
        return base
    scaled = int(round(base * (float(hard_s) / float(reference))))
    return max(base - NEUTRAL_RPE_MAX_DISCOUNT, min(base, scaled))


def apply_rpe_ftp_feedback(
    user_id: int, now: Optional[_dt.datetime] = None
) -> Optional[float]:
    """Apply one bounded FTP step from multiple new compliant hard workouts.

    Expected effort is type- and dose-aware: VO2 RPE 9 is neutral and threshold
    and sweet-spot RPE 8 are neutral for a session that delivered its full time
    in zone, scaled down for one that fell short (see ``_neutral_rpe``).
    Evidence is consumed in reversible batches.

    The step size is driven by how far RPE sits from neutral, not by the
    workout's own realized wattage: an ERG-controlled ride tracks its target
    power almost exactly, so "effective_ftp" (actual/target) mostly just
    reflects the FTP the workout was generated from and can't reveal that the
    prescribed intensity felt too easy - which is exactly the case a returning
    rider needs this to catch. Wattage evidence still applies (via max()) so a
    genuinely higher realized effort (e.g. non-ERG, free ride) isn't ignored.

    A rider who has set a manual FTP gets a *suggestion* instead: the same
    evidence, the same arithmetic, filed via ``db.record_ftp_suggestion`` for
    them to accept or dismiss. The training FTP never moves on its own while a
    manual value is set - the point of setting one is that the rider decides -
    but the evidence they gave is no longer thrown away. Returns the new FTP
    when one was applied, and None otherwise (including the manual case, where
    by definition nothing was applied).
    """
    now = now or utc_now()
    settings = db.get_user_settings(user_id)
    manual = bool(settings.get("ftp") and float(settings["ftp"]) > 0)
    latest = db.latest_ftp(user_id)
    if manual:
        # Judge the evidence against the FTP the rider actually trained at -
        # their manual value - not against a shadow estimate they never used.
        current = float(settings["ftp"])
        ftp_date = (latest or {}).get("date") or now.date().isoformat()
    else:
        if not latest or latest.get("source") != "estimated":
            return None
        current = float(latest["ftp_watts"])
        ftp_date = latest["date"]
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
        item["neutral_rpe"] = _neutral_rpe(
            item["type"], _zwo_hard_seconds(item.get("zwo"))
        )
        evidence.append(item)
    # One point either side of the session's own neutral. At a full dose these
    # are the same fixed bands as before (7/9 for threshold and sweet spot,
    # 8/10 for VO2max); a truncated session simply expects less.
    low = [e for e in evidence if int(e["rpe"]) <= e["neutral_rpe"] - 1]
    high = [e for e in evidence if int(e["rpe"]) >= e["neutral_rpe"] + 1]
    chosen: List[dict]
    if len(low) >= FTP_FEEDBACK_MIN_WORKOUTS:
        chosen = low
        demonstrated = float(np.median([e["effective_ftp"] for e in chosen]))
        rpe_gap = float(np.median(
            [e["neutral_rpe"] - int(e["rpe"]) for e in chosen]
        ))
        rpe_implied = current * (1.0 + FTP_RPE_STEP_PER_POINT * max(0.0, rpe_gap))
        desired = max(current, demonstrated, rpe_implied)
    elif len(high) >= FTP_FEEDBACK_MIN_WORKOUTS:
        chosen = high
        rpe_gap = float(np.median(
            [int(e["rpe"]) - e["neutral_rpe"] for e in chosen]
        ))
        desired = current * (1.0 - FTP_RPE_STEP_PER_POINT * max(0.0, rpe_gap))
    else:
        return None
    limit = current * FTP_FEEDBACK_MAX_STEP
    updated = max(50.0, min(600.0, current + max(-limit, min(limit, desired - current))))
    updated = round(updated, 1)
    delta = round(updated - current, 1)
    if manual:
        # Consume the evidence either way: a suggestion the rider dismisses
        # must not reappear from the same workouts, and evidence that implies
        # no change has still been used up.
        db.record_ftp_suggestion(
            user_id, ftp_date, current, updated, chosen,
            summary=[_evidence_summary(e) for e in chosen],
        )
        return None
    if abs(updated - current) < 0.1:
        batch = db.apply_feedback_batch(
            user_id, ftp_date, current, 0.0, chosen
        )
        return current if batch is not None else None
    batch = db.apply_feedback_batch(
        user_id, ftp_date, updated, delta, chosen
    )
    return updated if batch is not None else None


def _evidence_summary(item: dict) -> dict:
    """The human-readable part of one piece of evidence, for the UI."""
    return {
        "kind": item.get("kind"),
        "id": int(item["id"]),
        "date": item.get("completed_date"),
        "type": item.get("type"),
        "rpe": int(item["rpe"]),
        "neutral_rpe": int(item.get("neutral_rpe") or 0),
        "hard_minutes": int(round(_zwo_hard_seconds(item.get("zwo")) / 60.0)),
    }


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
            # Then let unspent RPE evidence have its say. Ratings already
            # trigger this, so for most users it is a no-op; it exists for the
            # backlog - riders whose ratings were silently discarded before
            # manual FTP produced a suggestion, and anyone who rated while a
            # completion was still unverified. Runs after evaluate_ftp so the
            # step lands on the row that re-evaluation just settled.
            apply_rpe_ftp_feedback(uid, now)
        except Exception:
            pass  # a broken folder for one user must not stop the sweep
    return totals
