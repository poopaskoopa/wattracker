"""Zwift race results, keyed by the numeric Zwift rider/player ID.

Data-source reality (verified 2026-07): there is currently NO public no-auth
API for Zwift race results:
  - zwiftpower.com/cache3/profile/{id}_all.json -> 403 "MissingKey"
    (CloudFront signed cookies, i.e. a logged-in browser session)
  - zwiftpower.com/api3.php?do=profile_results  -> serves the login page
  - zwiftracing.app/api/riders/{id}             -> 403 "Request origin not
    allowed" (API key / origin gated)
  - zwift-ranking.herokuapp.com                 -> app gone (404)

The remote fetch below still TRIES ZwiftPower first (cheap, and it degrades
gracefully if Zwift ever re-opens the endpoint), but the reliable source is
the documented fallback: races are derived from the user's imported FIT rides
with a simple heuristic (sustained intensity factor >= RACE_IF_MIN over a
15 min - 3 h ride). The UI labels the source explicitly.

Power-per-period durations are fixed by spec: 1s/5s/10s/30s/1m/2m/5m/10m/
20m/40m/60m.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from . import db
from .metrics.curve import best_rolling_power

log = logging.getLogger(__name__)

# Exactly the spec'd table columns (seconds).
RACE_POWER_DURATIONS = (1, 5, 10, 30, 60, 120, 300, 600, 1200, 2400, 3600)
DURATION_LABELS = {1: "1s", 5: "5s", 10: "10s", 30: "30s", 60: "1m", 120: "2m",
                   300: "5m", 600: "10m", 1200: "20m", 2400: "40m", 3600: "1h"}

# Local race heuristic: hard, sustained, race-length efforts.
RACE_IF_MIN = 0.83
RACE_MIN_S = 15 * 60
RACE_MAX_S = 3 * 3600

ZWIFTPOWER_PROFILE_URL = "https://zwiftpower.com/cache3/profile/{rider_id}_all.json"
_TIMEOUT_S = 12


class RaceSourceUnavailable(Exception):
    """The remote race-results source cannot be used (auth wall, offline...)."""


def _http_get_json(url: str, timeout: float = _TIMEOUT_S) -> dict:
    """GET a JSON document (no auth). Raises RaceSourceUnavailable on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "TRanalyzer/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise RaceSourceUnavailable(f"unreachable: {e}") from e
    if "json" not in ctype:
        raise RaceSourceUnavailable(f"non-JSON response ({ctype or 'unknown type'})")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise RaceSourceUnavailable(f"bad JSON: {e}") from e


def fetch_zwiftpower_results(rider_id: str) -> List[dict]:
    """Fetch race results from ZwiftPower's cached profile JSON.

    NOTE: gated behind a login cookie as of 2026-07 (403 MissingKey), so this
    normally raises RaceSourceUnavailable; kept because it needs no scraping
    and self-heals if the endpoint opens up again.
    """
    doc = _http_get_json(ZWIFTPOWER_PROFILE_URL.format(rider_id=rider_id))
    rows = doc.get("data") or []
    out: List[dict] = []
    fetched = _dt.datetime.now().isoformat(timespec="seconds")
    for r in rows:
        try:
            when = _dt.datetime.fromtimestamp(int(r.get("event_date") or 0))
        except (ValueError, TypeError, OSError):
            continue
        out.append(
            {
                "event_date": when.date().isoformat(),
                "event_title": str(r.get("event_title") or "Zwift event"),
                "position": str(r.get("position_in_cat") or r.get("pos") or ""),
                "category": str(r.get("category") or ""),
                "activity_id": None,
                "duration_s": None,
                "avg_power": _f(r.get("avg_power")),
                "np": _f(r.get("np")),
                "if_": None,
                "power": {},
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


def power_per_period(stream: List[float]) -> Dict[str, int]:
    """Best average power for each spec'd duration available in the stream."""
    out: Dict[str, int] = {}
    if not stream:
        return out
    for d in RACE_POWER_DURATIONS:
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
    fetched = _dt.datetime.now().isoformat(timespec="seconds")
    out: List[dict] = []
    for a in db.full_activities(user_id):
        if not _is_race(a):
            continue
        stream = (a.get("streams") or {}).get("power") or []
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
                "np": a.get("np"),
                "if_": a.get("if_"),
                "power": power_per_period(stream),
                "fetched_at": fetched,
            }
        )
    out.sort(key=lambda r: r["event_date"], reverse=True)
    return out


def compute_bests(user_id: int) -> Dict[str, int]:
    """All-time best power per spec'd duration across every imported ride."""
    bests: Dict[str, int] = {}
    for a in db.full_activities(user_id):
        stream = (a.get("streams") or {}).get("power") or []
        for key, watts in power_per_period(stream).items():
            if watts > bests.get(key, 0):
                bests[key] = watts
    return bests


def refresh_race_results(user_id: int, rider_id: Optional[str] = None) -> Dict:
    """Refresh the user's cached race results (remote first, local fallback).

    Stores results + sync metadata in the DB and returns a summary dict:
    {source, count, error (remote failure reason or None)}.
    """
    db.init_db()
    if rider_id is None:
        rider_id = (db.get_user_settings(user_id).get("zwift_id") or "").strip()
    rider_id = (rider_id or "").strip()

    remote_error: Optional[str] = None
    results: List[dict] = []
    source = "local"
    if rider_id.isdigit():
        try:
            results = fetch_zwiftpower_results(rider_id)
            source = "zwiftpower"
        except RaceSourceUnavailable as e:
            remote_error = str(e)
    else:
        remote_error = "no numeric Zwift rider ID configured"

    if source != "zwiftpower":
        results = derive_local_results(user_id)

    # Remote rows carry no power stream; fill per-race power from the local
    # ride imported on the same date, when we have one.
    if source == "zwiftpower":
        for r in results:
            if r["power"]:
                continue
            same_day = db.activities_on_date(user_id, r["event_date"])
            if same_day:
                act = db.get_activity(user_id, same_day[0]["id"])
                stream = (act.get("streams") or {}).get("power") or []
                r["power"] = power_per_period(stream)
                r["activity_id"] = act["id"]

    count = db.replace_race_results(user_id, source, results)
    db.save_race_sync(
        user_id, rider_id or None, source, remote_error,
        bests=compute_bests(user_id),
    )
    return {"source": source, "count": count, "error": remote_error}


def race_page_data(user_id: int) -> Dict:
    """Everything the /races page needs from the cache (no network)."""
    sync = db.get_race_sync(user_id)
    results = db.list_race_results(user_id)
    return {
        "sync": sync,
        "results": results,
        "bests": (sync or {}).get("bests") or {},
        "durations": [str(d) for d in RACE_POWER_DURATIONS],
        "duration_labels": [DURATION_LABELS[d] for d in RACE_POWER_DURATIONS],
    }
