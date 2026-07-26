"""Zwift race results, keyed by the numeric Zwift rider/player ID.

Data-source reality (verified 2026-07): there is currently NO public no-auth
API for Zwift race results:
  - zwiftpower.com/cache3/profile/{id}_all.json -> 403 "MissingKey"
    (CloudFront signed cookies, i.e. a logged-in browser session)
  - zwiftpower.com/api3.php?do=profile_results  -> serves the login page
  - zwiftracing.app/api/riders/{id}             -> 403 "Request origin not
    allowed" (API key / origin gated)
  - zwift-ranking.herokuapp.com                 -> app gone (404)

Since every user of this app has a Zwift login, the PRIMARY source is an
authenticated ZwiftPower fetch using the user's own saved credentials (see
``zwiftauth``: Zwift SSO password grant + the ZwiftPower SSO cookie dance),
which unlocks the same cache3 JSON endpoints with real placements/categories.
Fallbacks, in order: anonymous cache3 fetch (dead today, self-healing), then
races derived from the user's imported FIT rides with a simple heuristic
(sustained intensity factor >= RACE_IF_MIN over a 15 min - 3 h ride). The UI
labels the active source explicitly, including "login failed".

Power-per-period durations are fixed by spec: 1s/5s/10s/30s/1m/2m/5m/10m/
20m/40m/60m.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from . import db
from .timeutil import utc_now, utc_today
from .metrics.curve import best_rolling_power

log = logging.getLogger(__name__)

# Per-race table columns (seconds), spec: 1s/5s/15s/30s/1m/2m/5m/10m/20m.
RACE_POWER_DURATIONS = (1, 5, 15, 30, 60, 120, 300, 600, 1200)
# ZwiftPower publishes per-result peak power as ``w<seconds>`` fields; this maps
# the ones it carries to our grid (it lacks 1s and 10m, filled from local rides).
ZP_POWER_FIELDS = {5: "w5", 15: "w15", 30: "w30", 60: "w60", 120: "w120",
                   300: "w300", 1200: "w1200"}
# The "Power profile" section durations (spec: 1s/5s/15s/30s/1m/2m/5m/10m/20m/
# 40m/1h) - all-time bests across every imported ride.
PROFILE_DURATIONS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 2400, 3600)
# Union grid used when computing/caching bests so both tables can render.
BESTS_DURATIONS = tuple(sorted(set(RACE_POWER_DURATIONS) | set(PROFILE_DURATIONS)))
DURATION_LABELS = {1: "1s", 5: "5s", 10: "10s", 15: "15s", 30: "30s", 60: "1m",
                   120: "2m", 300: "5m", 600: "10m", 1200: "20m", 2400: "40m",
                   3600: "1h"}

# Local race heuristic: hard, sustained, race-length efforts.
RACE_IF_MIN = 0.83
RACE_MIN_S = 15 * 60
RACE_MAX_S = 3 * 3600

ZWIFTPOWER_PROFILE_URL = "https://zwiftpower.com/cache3/profile/{rider_id}_all.json"
_TIMEOUT_S = 12


class RaceSourceUnavailable(Exception):
    """The remote race-results source cannot be used (auth wall, offline...)."""


def _http_get_json(url: str, timeout: float = _TIMEOUT_S) -> dict:
    """GET a ZwiftPower cache JSON document without credentials.

    The ``cache3`` profile JSON is served by CloudFront behind signed cookies
    (``CloudFront-Policy`` / ``-Signature`` / ``-Key-Pair-Id``); a bare request
    gets ``403 MissingKey``. Those cookies are minted by ZwiftPower's OAuth-SSO
    round-trip, so we open an anonymous ZwiftPower session first and fetch the
    JSON through its cookie jar. Raises RaceSourceUnavailable on failure.
    """
    from . import zwiftauth  # local import: avoids an import cycle at load time
    try:
        opener = zwiftauth.zwiftpower_session(timeout=timeout)[0]
        with opener.open(url, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read()
    except zwiftauth.ZwiftAuthError as e:
        raise RaceSourceUnavailable(str(e)) from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise RaceSourceUnavailable(f"unreachable: {e}") from e
    if "json" not in ctype:
        raise RaceSourceUnavailable(f"non-JSON response ({ctype or 'unknown type'})")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise RaceSourceUnavailable(f"bad JSON: {e}") from e


def fetch_zwiftpower_results(rider_id: str) -> List[dict]:
    """Fetch race results from ZwiftPower's cached profile JSON, anonymously.

    NOTE: gated behind a login cookie as of 2026-07 (403 MissingKey), so this
    normally raises RaceSourceUnavailable; kept as a last resort for users
    without saved credentials (self-heals if the endpoint opens up again).
    """
    doc = _http_get_json(ZWIFTPOWER_PROFILE_URL.format(rider_id=rider_id))
    return parse_zwiftpower_profile(doc)


def _is_zp_race(row: dict) -> bool:
    """A ZwiftPower result is a race iff its event-type flags contain TYPE_RACE.

    ZwiftPower tags every result with ``f_t`` (e.g. ``"TYPE_RACE TYPE_RACE "``
    for races, ``"TYPE_RIDE"`` for group rides, ``"TYPE_WORKOUT ..."`` for
    workouts). Group rides and workouts are excluded here.
    """
    return "TYPE_RACE" in str(row.get("f_t") or "")


def _zp_power_periods(row: dict) -> Dict[str, int]:
    """Peak power per period from a ZwiftPower result's ``w<seconds>`` fields."""
    out: Dict[str, int] = {}
    for secs, field in ZP_POWER_FIELDS.items():
        w = _f(row.get(field))
        if w and w > 0:
            out[str(secs)] = int(round(w))
    return out


def parse_zwiftpower_profile(doc: dict) -> List[dict]:
    """Normalize a ZwiftPower profile JSON document into race result rows.

    Only actual races are kept (see ``_is_zp_race``); each row carries its raw
    ``f_t`` in ``source_type`` so the filter stays auditable, plus the per-race
    peak-power periods ZwiftPower publishes (``w5``..``w1200``).
    """
    rows = doc.get("data") or []
    out: List[dict] = []
    fetched = utc_now().isoformat(timespec="seconds")
    for r in rows:
        if not _is_zp_race(r):
            continue
        try:
            when = _dt.datetime.fromtimestamp(
                int(r.get("event_date") or 0), _dt.timezone.utc
            ).replace(tzinfo=None)
        except (ValueError, TypeError, OSError):
            continue
        dur = _f(r.get("time"))  # seconds ([value, flag] or scalar)
        np = _f(r.get("np"))
        # ZwiftPower does not publish IF, but it does carry the rider's FTP at
        # the race (``ftp`` / ``wftp``); IF = NP / FTP. When that FTP is
        # missing/zero it is filled later from ftp_history as-of the race date.
        zp_ftp = _f(r.get("ftp")) or _f(r.get("wftp"))
        if_ = round(np / zp_ftp, 2) if (np and zp_ftp and zp_ftp > 0) else None
        # ZwiftPower exposes race distance in kilometres (integer field).
        dist = _f(r.get("distance"))
        out.append(
            {
                "event_date": when.date().isoformat(),
                "event_title": str(r.get("event_title") or "Zwift event"),
                "position": str(r.get("position_in_cat") or r.get("pos") or ""),
                "category": str(r.get("category") or ""),
                "source_type": str(r.get("f_t") or "").strip(),
                "activity_id": None,
                "duration_s": int(dur) if dur and dur > 0 else None,
                "avg_power": _f(r.get("avg_power")),
                "avg_hr": _bounded(r.get("avg_hr"), 20, 250),
                "max_hr": _bounded(r.get("max_hr"), 20, 250),
                "weight_kg": _bounded(r.get("weight"), 20, 300),
                "np": np,
                "if_": if_,
                "distance_km": dist if dist and dist > 0 else None,
                # ZwiftPower event id (``zid``) links each race to its results
                # page; encoded like other ZP fields (scalar or [value, flag]).
                "zp_event_id": _zid(r.get("zid")),
                "power": _zp_power_periods(r),
                "fetched_at": fetched,
            }
        )
    return out


def _f(v) -> Optional[float]:
    # ZwiftPower encodes numbers as [value, flag] pairs or plain scalars.
    if isinstance(v, (list, tuple)) and v:
        v = v[0]
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bounded(v, low: float, high: float) -> Optional[float]:
    """A finite ZwiftPower number inside an inclusive plausible range."""
    value = _f(v)
    if value is None or not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def _fit_hr(activity: dict) -> tuple[Optional[float], Optional[float]]:
    """Validated average/max HR from an imported FIT activity."""
    avg_hr = _bounded(activity.get("avg_hr"), 20, 250)
    raw = ((activity.get("streams") or {}).get("heartrate") or [])
    samples = [_bounded(value, 20, 250) for value in raw]
    valid = [value for value in samples if value is not None]
    return avg_hr, max(valid) if valid else None


def _zid(v) -> Optional[str]:
    """A ZwiftPower event id as a digit string, or None.

    ``zid`` may be an int, a numeric string, or a ``[value, flag]`` list like
    other ZwiftPower fields; anything non-numeric (or 0) yields None.
    """
    if isinstance(v, (list, tuple)) and v:
        v = v[0]
    if v is None:
        return None
    s = str(v).strip()
    return s if s.isdigit() and int(s) > 0 else None


def power_per_period(
    stream: List[float], durations=RACE_POWER_DURATIONS
) -> Dict[str, int]:
    """Best average power for each spec'd duration available in the stream."""
    out: Dict[str, int] = {}
    if not stream:
        return out
    for d in durations:
        if len(stream) >= d:
            best = best_rolling_power(stream, d)
            if best > 0:
                out[str(d)] = int(round(best))
    return out


def _is_race(activity: dict) -> bool:
    dur = activity.get("duration_s") or 0
    if not (RACE_MIN_S <= dur <= RACE_MAX_S):
        return False
    return (activity.get("if_") or 0.0) >= RACE_IF_MIN


def derive_local_results(user_id: int) -> List[dict]:
    """Derive race results from imported FIT rides (heuristic, labeled as such)."""
    fetched = utc_now().isoformat(timespec="seconds")
    out: List[dict] = []
    for a in db.full_activities(user_id):
        if not _is_race(a):
            continue
        stream = (a.get("streams") or {}).get("power") or []
        avg_hr, max_hr = _fit_hr(a)
        date = (a.get("start_time") or "")[:10] or "unknown"
        title = (a.get("filename") or "ride").rsplit(".", 1)[0]
        out.append(
            {
                "event_date": date,
                "event_title": f"Race effort - {title}",
                "position": None,
                "category": None,
                "activity_id": a["id"],
                "duration_s": a.get("duration_s"),
                "avg_power": a.get("avg_power"),
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "weight_kg": None,
                "np": a.get("np"),
                "if_": a.get("if_"),
                "power": power_per_period(stream),
                "fetched_at": fetched,
            }
        )
    out.sort(key=lambda r: r["event_date"], reverse=True)
    return out


def compute_bests(user_id: int) -> Dict[str, int]:
    """All-time best power across every imported ride, over the union grid
    (per-race table durations + Power-profile durations, incl. 15s)."""
    bests: Dict[str, int] = {}
    for a in db.full_activities(user_id):
        stream = (a.get("streams") or {}).get("power") or []
        for key, watts in power_per_period(stream, BESTS_DURATIONS).items():
            if watts > bests.get(key, 0):
                bests[key] = watts
    return bests


def refresh_race_results(
    user_id: int,
    rider_id: Optional[str] = None,
    respect_backoff: bool = False,
) -> Dict:
    """Refresh the user's cached race results.

    Source order:
      1. Authenticated ZwiftPower fetch (real results, incl. placement and
         category) when the user has Zwift credentials saved - a single auth
         attempt per refresh; when the credentials were rejected on a previous
         attempt and ``respect_backoff`` is set (the daily sweep), the attempt
         is skipped until the user refreshes manually or re-saves credentials,
         so retry storms can never lock the Zwift account.
      2. Anonymous ZwiftPower fetch (no credentials + numeric rider id; gated
         by Zwift's login wall as of 2026-07, kept as a self-healing fallback).
      3. Local FIT-derived race efforts.

    Stores results + sync metadata in the DB and returns a summary dict:
    {source, count, error (remote failure reason or None), auth_failed}.

    Real results replace heuristics, never mix with them: a successful
    ZwiftPower fetch PURGES any previously-persisted FIT-derived rows, and
    once real results are cached a later failed refresh keeps them (stale)
    instead of regenerating heuristic entries.
    """
    from . import credstore, zwiftauth  # local import: keeps db-only callers light

    db.init_db()
    if rider_id is None:
        rider_id = (db.get_user_settings(user_id).get("zwift_id") or "").strip()
    rider_id = (rider_id or "").strip()

    remote_error: Optional[str] = None
    auth_failed = False
    weight_kg: Optional[float] = None
    results: List[dict] = []
    source = "local"
    creds = credstore.get_zwift_credentials(user_id)

    if creds is not None:
        prev = db.get_race_sync(user_id)
        if respect_backoff and prev and prev.get("auth_failed"):
            remote_error = (
                "Zwift login previously failed - automatic fetching is paused "
                "until you re-save credentials or refresh manually"
            )
            auth_failed = True
        else:
            try:
                doc, detected_id, weight_kg = zwiftauth.fetch_results_authenticated(
                    creds.email, creds.password,
                    rider_id if rider_id.isdigit() else None,
                )
                results = parse_zwiftpower_profile(doc)
                source = "zwiftpower"
                if detected_id and detected_id != rider_id:
                    # Auto-detected from the Zwift profile: persist it.
                    rider_id = detected_id
                    db.save_user_settings(user_id, {"zwift_id": detected_id})
                if weight_kg is None:
                    weight_kg = _weight_from_zwiftpower_doc(doc)
            except zwiftauth.ZwiftAuthError as e:
                remote_error = str(e)
                auth_failed = bool(getattr(e, "credential_problem", False))
    elif rider_id.isdigit():
        try:
            results = fetch_zwiftpower_results(rider_id)
            source = "zwiftpower"
        except RaceSourceUnavailable as e:
            remote_error = str(e)
    else:
        remote_error = ("no numeric Zwift rider ID configured and no Zwift "
                        "credentials saved")

    # Weight (for W/kg display): Zwift profile is primary, ZwiftPower rows
    # secondary; refreshed on every successful authenticated refresh.
    if weight_kg:
        db.save_user_settings(user_id, {"weight_kg": weight_kg})

    if source == "zwiftpower":
        # Resolve each race to the local ride of that date so the row links to
        # its ride detail; also fill any power period ZwiftPower doesn't publish
        # (1s / 10m) from that ride's stream.
        for r in results:
            # IF that ZwiftPower's per-race FTP didn't yield: compute from NP
            # and the user's FTP as-of the race date (a local record).
            if r.get("if_") is None and r.get("np"):
                ftp = db.ftp_as_of(user_id, r["event_date"])
                if ftp and ftp > 0:
                    r["if_"] = round(r["np"] / ftp, 2)
            act = _matching_activity(user_id, r["event_date"], r.get("duration_s"))
            if act is None:
                continue
            r["activity_id"] = act["id"]
            fit_avg_hr, fit_max_hr = _fit_hr(act)
            if r.get("avg_hr") is None:
                r["avg_hr"] = fit_avg_hr
            if r.get("max_hr") is None:
                r["max_hr"] = fit_max_hr
            # Backfill distance from the local ride when ZwiftPower omits it.
            if not r.get("distance_km") and act.get("distance_m"):
                r["distance_km"] = round(act["distance_m"] / 1000.0, 1)
            missing = [d for d in RACE_POWER_DURATIONS if str(d) not in r["power"]]
            if missing:
                stream = (act.get("streams") or {}).get("power") or []
                for key, watts in power_per_period(stream, missing).items():
                    r["power"].setdefault(key, watts)
        count = db.replace_race_results(user_id, "zwiftpower", results)
        # Real results supersede the heuristic ones: purge them for good.
        db.delete_race_results(user_id, "local")
    elif db.count_race_results(user_id, "zwiftpower") > 0:
        # Refresh failed but real results are cached: keep them (stale)
        # rather than fabricating heuristic rows next to real races.
        source = "zwiftpower"
        count = db.count_race_results(user_id, "zwiftpower")
    else:
        results = derive_local_results(user_id)
        count = db.replace_race_results(user_id, "local", results)

    db.save_race_sync(
        user_id, rider_id or None, source, remote_error,
        bests=compute_bests(user_id), auth_failed=auth_failed,
    )
    return {"source": source, "count": count, "error": remote_error,
            "auth_failed": auth_failed}


def _matching_activity(
    user_id: int, date_iso: str, duration_s: Optional[int]
) -> Optional[dict]:
    """The imported ride best matching a race on a date (closest duration)."""
    cands = db.activities_on_date(user_id, date_iso)
    if not cands:
        return None
    if duration_s and len(cands) > 1:
        cands = sorted(
            cands, key=lambda a: abs((a.get("duration_s") or 0) - int(duration_s))
        )
    return db.get_activity(user_id, cands[0]["id"])


# ------------------------------------------------- planned-vs-actual linking
# `race_dates` (future intent, see db.py's DDL comment) and `race_results`
# (past fact, refreshed wholesale from ZwiftPower/local heuristics - see
# `refresh_race_results`) are deliberately separate tables. The association
# below is resolved at READ time rather than stored as a foreign key on
# `race_dates`: `replace_race_results` DELETEs and re-INSERTs every row for a
# source on each refresh, so a `race_results.id` is not stable across
# refreshes - a persisted FK would go stale the moment results are next
# fetched and would need re-resolving anyway, so a live resolver is strictly
# simpler and can never point at a row that no longer exists. Matching is
# also cheap: at most a handful of rows share a (user_id, event_date).


def _result_match_score(planned: dict, candidate: dict) -> tuple:
    """Lower is better. Tie-break for >1 race_results row on the same date.

    Preference order:
      1. Title overlap: the planned race's ``name`` and the result's
         ``event_title`` share text (case-insensitive substring either way).
         This is the strongest positive signal - a rider who named their
         planned race after the actual event is telling us which result is
         theirs.
      2. Closest duration: |result duration - planned duration_min| in
         minutes. Absent a duration on either side sorts last (treated as an
         infinite gap) rather than winning by default.
      3. Lowest `race_results.id`, purely for determinism when the above are
         still tied (e.g. two untitled, undurationed results on one date) -
         arbitrary, but stable and never crashes on a genuine tie.
    """
    name = (planned.get("name") or "").strip().lower()
    title = (candidate.get("event_title") or "").strip().lower()
    title_match = 0 if name and title and (name in title or title in name) else 1

    planned_min = planned.get("duration_min")
    cand_s = candidate.get("duration_s")
    if planned_min and cand_s:
        duration_gap = abs(cand_s / 60.0 - float(planned_min))
    else:
        duration_gap = math.inf

    return (title_match, duration_gap, candidate.get("id") or 0)


def _resolvable_race_date(race_date: dict) -> Optional[str]:
    """The date to look results up on, or None if it can't have one yet.

    A race in the future has not been ridden, so it is never worth a lookup -
    and a stray same-date result from a previous year's edition of the event
    must not be presented as this year's outcome.
    """
    date_iso = race_date.get("date") or ""
    if not date_iso or date_iso > utc_today().isoformat():
        return None
    return date_iso


def _best_result(planned: dict, candidates: List[dict]) -> Optional[dict]:
    """The candidate that best matches ``planned`` - see _result_match_score."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return min(candidates, key=lambda c: _result_match_score(planned, c))


def _result_for_display(result: Optional[dict]) -> Optional[dict]:
    """A matched result with the same derived fields /races renders.

    A linked result should read identically wherever it appears, so `place`
    and `duration_fmt` are derived here exactly as `race_page_data` does
    rather than re-implemented in each template.
    """
    if result is None:
        return None
    out = dict(result)
    out["place"] = _place_int(out.get("position"))
    out["duration_fmt"] = format_duration(out.get("duration_s"))
    return out


def match_result_for_race_date(
    user_id: int, race_date: dict
) -> Optional[dict]:
    """The cached race result matching a planned race date, or None.

    None is the ordinary case: most planned races are in the future, or the
    rider hasn't refreshed race results since racing it. This never raises -
    an unmatched or ambiguous race must never break a calendar/races-page
    render.

    Single-race helper. Resolving a whole list goes through
    ``attach_results_to_race_dates``, which shares one query across them.
    """
    date_iso = _resolvable_race_date(race_date)
    if not date_iso:
        return None
    try:
        candidates = db.race_results_on_date(user_id, date_iso)
    except Exception:  # noqa: BLE001 - a lookup failure just means "no link"
        log.warning("race result lookup failed for a planned race date",
                    exc_info=True)
        return None
    return _result_for_display(_best_result(race_date, candidates))


def attach_results_to_race_dates(
    user_id: int, race_dates: List[dict]
) -> List[dict]:
    """Copies of ``race_dates`` rows, each with a ``result`` key attached.

    ``result`` is the matched ``race_results`` row (or None). Idempotent and
    side-effect free - it only reads, never writes `race_dates`.

    One query serves every race: the calendar resolves the rider's whole
    planned-race list on every render, and going per-race meant a fresh
    sqlite connection per planned race for a page that is already the app's
    heaviest read.
    """
    dates = {d for d in (_resolvable_race_date(r) for r in race_dates) if d}
    by_date: Dict[str, List[dict]] = {}
    if dates:
        try:
            by_date = db.race_results_on_dates(user_id, sorted(dates))
        except Exception:  # noqa: BLE001 - a lookup failure just means "no link"
            log.warning("race result lookup failed for planned race dates",
                        exc_info=True)
            by_date = {}
    out = []
    for r in race_dates:
        d = dict(r)
        date_iso = _resolvable_race_date(r)
        d["result"] = _result_for_display(
            _best_result(r, by_date.get(date_iso) or []) if date_iso else None
        )
        out.append(d)
    return out


def _weight_from_zwiftpower_doc(doc: dict) -> Optional[float]:
    """Secondary weight source: the most recent ZwiftPower result row (kg)."""
    rows = doc.get("data") or []
    best = None
    for r in rows:
        w = _bounded(r.get("weight"), 20, 300)
        when = r.get("event_date") or 0
        if w is not None:
            if best is None or when >= best[0]:
                best = (when, w)
    return round(best[1], 1) if best else None


def format_duration(seconds: Optional[float]) -> Optional[str]:
    """Format a duration in seconds as ``h:mm:ss`` (>=1h) or ``mm:ss`` (<1h),
    rounded to the nearest second."""
    if seconds is None:
        return None
    total_s = round(float(seconds))
    hours, rem_s = divmod(total_s, 3600)
    minutes, secs = divmod(rem_s, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _place_int(position) -> Optional[int]:
    """Parse a race position field to an int (handles '1', 1, '1st', '3 /40')."""
    if position is None:
        return None
    m = re.match(r"\s*(\d+)", str(position))
    return int(m.group(1)) if m else None


def race_page_data(user_id: int) -> Dict:
    """Everything the /races page needs from the cache (no network)."""
    sync = db.get_race_sync(user_id)
    results = db.list_race_results(user_id)
    # Real results and heuristics never mix on the page: if any ZwiftPower
    # rows exist, heuristic rows are hidden (and purged at next refresh).
    if any(r["source"] == "zwiftpower" for r in results):
        results = [r for r in results if r["source"] == "zwiftpower"]
    # Lazily backfill fields for rows cached before this logic existed:
    #  - activity_id so each race links to its detail graphs;
    #  - IF (= NP / FTP as-of the race date) which ZwiftPower never provides.
    for r in results:
        act = None
        if not r.get("activity_id"):
            act = _matching_activity(user_id, r["event_date"], r.get("duration_s"))
            if act is not None:
                r["activity_id"] = act["id"]
        if not r.get("distance_km"):
            # Backfill distance from the matching local ride (metres -> km).
            if act is None and r.get("activity_id"):
                act = db.get_activity(user_id, r["activity_id"])
            if act and act.get("distance_m"):
                r["distance_km"] = round(act["distance_m"] / 1000.0, 1)
        if r.get("avg_hr") is None or r.get("max_hr") is None:
            if act is None and r.get("activity_id"):
                act = db.get_activity(user_id, r["activity_id"])
            if act:
                fit_avg_hr, fit_max_hr = _fit_hr(act)
                if r.get("avg_hr") is None:
                    r["avg_hr"] = fit_avg_hr
                if r.get("max_hr") is None:
                    r["max_hr"] = fit_max_hr
        if r.get("if_") is None and r.get("np"):
            ftp = db.ftp_as_of(user_id, r["event_date"])
            if ftp and ftp > 0:
                r["if_"] = round(r["np"] / ftp, 2)
        r["place"] = _place_int(r.get("position"))
        r["duration_fmt"] = format_duration(r.get("duration_s"))
    weight = db.get_user_settings(user_id).get("weight_kg")
    return {
        "sync": sync,
        "results": results,
        "bests": (sync or {}).get("bests") or {},
        "durations": [str(d) for d in RACE_POWER_DURATIONS],
        "duration_labels": [DURATION_LABELS[d] for d in RACE_POWER_DURATIONS],
        "profile_durations": [str(d) for d in PROFILE_DURATIONS],
        "profile_labels": [DURATION_LABELS[d] for d in PROFILE_DURATIONS],
        "weight_kg": float(weight) if weight else None,
    }
