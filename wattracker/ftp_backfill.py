"""Explicit, offline repair for activity FTP-dependent metrics.

The command is intentionally separate from startup, migrations and every HTTP
route. It defaults to a read-only report; ``--write`` is required before it
creates a verified backup and changes the selected SQLite database. It
considers every activity row for every user, including duplicate rows; load
totals still follow the app's normal duplicate exclusion. Active power
corrections retain their stored power summary, while IF and TSS are rebased to
the FTP effective on the ride date.

What the report is for
----------------------
Deciding whether the repair is worth running at all. Two things make a naive
summary of this database misleading, so the report separates them out:

* **The corrupt population.** Rows scored against a sub-watt to few-watt FTP
  (#60) carry TSS values up to 1.6e7. Mixed into one distribution they dominate
  every statistic. They are selected by their *implied* scoring basis,
  ``np / if_``, not by a TSS threshold - a TSS cut-off silently misses the
  short rides among them.
* **The pre-``ftp_history`` rule.** ``ftp_history`` does not reach back to the
  start of the ride history, so for any ride older than a user's first recorded
  FTP the "FTP effective on that date" is the earliest FTP ever recorded,
  back-applied. That matches the app's existing ``ftp_as_of`` convention, but on
  this database it is the rule for almost every row, so the report states the
  count up front rather than leaving it implied.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sqlite3
import statistics
import sys
import tempfile
from typing import Dict, List, Optional, Sequence

from . import db
from .config import db_path
from .ftp_rescore import score_activity
from .metrics.load import compute_load, daily_tss_series
from .metrics.power import FTP_ASSERTION_MIN_WATTS, FTP_PLAUSIBLE_MIN_WATTS

_STATE_VERSION = 2
_COUNTED_TABLES = (
    "users",
    "activities",
    "ftp_history",
    "power_sample_corrections",
)

# A row whose implied scoring basis (np / if_) is below this was scored against
# a wattage this codebase now refuses to score against at all - the #60 damage.
# The line is the app's own estimate floor rather than a number invented here.
#
# It is applied as a plain numeric comparison, NOT via ``is_plausible_ftp``, on
# purpose: for a basis between FTP_ASSERTION_MIN_WATTS and this floor that
# function resolves provenance by opening the DEFAULT database, which for an
# offline report over a copy would be the wrong file and a writable handle on
# the live one. ``_reject_provenance_dependent_bases`` below makes sure the
# scoring path cannot reach that branch either. Provenance is in any case
# unknowable for a basis back-solved out of an old row.
CORRUPT_BASIS_MAX_WATTS = FTP_PLAUSIBLE_MIN_WATTS

# An IF this high is not a hard ride, it is a scoring error: 1.0 is threshold,
# and no rider sustains twice threshold for a whole activity. Issue #60 named
# this as the check that would have caught the damage years earlier. Rows above
# it whose basis is otherwise admissible are reported separately so they cannot
# quietly distort the ordinary distribution.
SUSPECT_IF = 2.0

_SKIP_NO_DATE = "no usable start_time"
_SKIP_NOT_SCOREABLE = "no admissible FTP basis or duration"


# --- backup, identity, progress ---------------------------------------------

def _integrity_and_counts(path: str) -> Dict[str, int]:
    conn = sqlite3.connect(db.read_only_uri(path), uri=True, timeout=10)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path}: {integrity}")
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _COUNTED_TABLES
        }
    finally:
        conn.close()


def _file_digest(path: str) -> str:
    """SHA-256 of the whole database file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _create_backup(source: str, destination: str) -> str:
    """Create and verify a fresh backup; refuse to reuse anything already there.

    Row-count equality is *not* an identity check - a differently populated
    database of the same shape passes it. The only file this tool will ever
    trust as "the backup of this run" is one it made itself and then recorded
    the digest of in the progress file, so an existing file here is an error
    rather than something to inspect and accept.
    """
    source = os.path.abspath(source)
    destination = os.path.abspath(destination)
    if source == destination:
        raise ValueError("backup path must differ from database path")
    if os.path.exists(destination):
        raise RuntimeError(
            f"refusing to reuse the existing file at {destination}: this tool "
            "cannot prove it is a backup of this database. Move it aside, or "
            "pass a different --backup path."
        )
    source_counts = _integrity_and_counts(source)
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    src = sqlite3.connect(db.read_only_uri(source), uri=True, timeout=10)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(destination, 0o600)
    if _integrity_and_counts(destination) != source_counts:
        raise RuntimeError("backup verification failed; original was not touched")
    return destination


def _verify_recorded_backup(state: dict, destination: str) -> str:
    """Check the resumed run's backup is byte-for-byte the one it started with."""
    recorded = state.get("backup")
    digest = state.get("backup_sha256")
    destination = os.path.abspath(destination)
    if recorded != destination:
        raise RuntimeError(
            f"progress file records a different backup: {recorded}"
        )
    if not os.path.exists(destination):
        raise RuntimeError(f"recorded backup is missing: {destination}")
    if not digest or _file_digest(destination) != digest:
        raise RuntimeError(
            f"backup at {destination} does not match the one this run created; "
            "it has been replaced or modified"
        )
    _integrity_and_counts(destination)
    return destination


def _load_state(path: str, source: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    if (
        state.get("version") != _STATE_VERSION
        or state.get("db") != os.path.abspath(source)
        or not isinstance(state.get("users"), dict)
    ):
        raise ValueError("progress file does not belong to this database")
    return state


def _save_state(path: str, state: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ftp-rescore-", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _clear_state(path: str) -> bool:
    """Remove a completed run's progress file.

    Leaving it behind makes the next run a no-op that exits 0 while the database
    is still uncorrected - which is exactly what happens after restoring from
    this tool's own backup.
    """
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


# --- populations -------------------------------------------------------------

def implied_basis(row: dict) -> Optional[float]:
    """The FTP a stored row was scored against, back-solved from ``if_ = np/ftp``.

    None when the row was never scored (``if_`` absent or 0), which is a third
    state - neither corrupt nor ordinary.
    """
    try:
        np_value = float(row.get("np") or 0.0)
        if_value = float(row.get("if_") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    if if_value <= 0 or np_value <= 0:
        return None
    return np_value / if_value


POPULATIONS = ("ordinary", "suspect", "corrupt", "unscored")


def classify(row: dict) -> str:
    """Which reporting population one stored activity row belongs to."""
    basis = implied_basis(row)
    if basis is None:
        return "unscored"
    if basis < CORRUPT_BASIS_MAX_WATTS:
        return "corrupt"
    if _number(row.get("if_")) > SUSPECT_IF:
        return "suspect"
    return "ordinary"


class _Stats:
    """Per-population counters and the deltas of the rows that actually change."""

    def __init__(self) -> None:
        self.rows = 0
        self.changed = 0
        self.unchanged = 0
        self.pre_history = 0
        self.deltas: List[float] = []

    def summary(self) -> dict:
        return {
            "rows": self.rows,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "pre_history": self.pre_history,
            "tss_delta": _distribution(self.deltas),
        }


def _distribution(values: Sequence[float]) -> dict:
    """min/median/max over *changed* rows only, or nulls when nothing changed.

    The two must describe the same population. Summarising every scored row
    while counting only the changed ones is how a clean re-run came to print
    "0 rows affected" beside "min/median/max 0.0/0.0/0.0".
    """
    if not values:
        return {"n": 0, "up": 0, "down": 0, "min": None, "median": None, "max": None}
    return {
        "n": len(values),
        "up": sum(1 for value in values if value > 0),
        "down": sum(1 for value in values if value < 0),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def _first_ftp_dates(source: str, users: Sequence[int], read_only: bool) -> Dict[int, Optional[_dt.date]]:
    conn = db.connect(source, read_only=read_only)
    try:
        out: Dict[int, Optional[_dt.date]] = {uid: None for uid in users}
        for row in conn.execute(
            "SELECT user_id, MIN(date) AS first_date FROM ftp_history GROUP BY user_id"
        ):
            try:
                out[int(row["user_id"])] = _dt.date.fromisoformat(str(row["first_date"]))
            except (ValueError, TypeError, KeyError):
                continue
        return out
    finally:
        conn.close()


# --- reporting ---------------------------------------------------------------

def _load_totals(daily: Dict[_dt.date, float]) -> Dict[str, float]:
    series = compute_load(daily_tss_series(daily))
    if not series:
        return {"ctl": 0.0, "atl": 0.0, "tsb": 0.0}
    last = series[-1]
    return {key: float(last[key]) for key in ("ctl", "atl", "tsb")}


def _fmt(value: Optional[float], places: int = 1) -> str:
    return "-" if value is None else f"{value:+.{places}f}"


def _print_report(result: dict) -> None:
    totals = result["totals"]
    ordinary = result["populations"]["ordinary"]["tss_delta"]
    mode = "write" if result["write"] else "dry-run"
    print(f"FTP backfill ({mode}) on {result['db']}")
    print()
    print(
        f"Rows examined {totals['rows_seen']}; "
        f"would change {totals['rows_changed']}; "
        f"unchanged {totals['rows_unchanged']}; "
        f"skipped {totals['rows_skipped']}"
    )
    print(
        "Scoring rule: FTP effective on the ride date, falling back to the "
        "earliest FTP ever recorded for rides older than that."
    )
    print(
        f"  -> {totals['pre_history_rows']} of {totals['rows_seen']} rows "
        f"({totals['pre_history_pct']:.1f}%) predate their user's first "
        "ftp_history entry and are therefore rescored against a back-applied "
        "earliest-ever FTP."
    )
    print()
    print("Populations (delta statistics cover changed rows only):")
    for name in POPULATIONS:
        pop = result["populations"][name]
        dist = pop["tss_delta"]
        print(
            f"  {name:<9} rows {pop['rows']:>6}  changed {pop['changed']:>6}  "
            f"(up {dist['up']}, down {dist['down']})  TSS delta min/median/max "
            f"{_fmt(dist['min'])}/{_fmt(dist['median'])}/{_fmt(dist['max'])}"
        )
    print(
        f"  (corrupt = implied basis np/if_ below {CORRUPT_BASIS_MAX_WATTS:.0f} W; "
        f"suspect = admissible basis but stored IF above {SUSPECT_IF:.1f}; "
        "unscored = if_ of 0, never scored)"
    )
    print()
    if ordinary["median"] is not None:
        direction = "UP" if ordinary["median"] > 0 else "DOWN"
        print(
            f"DIRECTION: for ordinary rows the median TSS change is "
            f"{ordinary['median']:+.1f} - stored TSS goes {direction}."
        )
        print()
    shift = result["load_shift"]
    print(
        "Summed CTL/ATL/TSB shift across users (corrupt and suspect rows "
        f"excluded from both sides): {_fmt(shift['ctl'], 2)}/"
        f"{_fmt(shift['atl'], 2)}/{_fmt(shift['tsb'], 2)}"
    )
    print()
    print("Per user (ord.* = ordinary population only; dCTL etc. exclude "
          "corrupt and suspect rows on both sides):")
    print(
        f"  {'uid':>4} {'rows':>6} {'chg':>6} {'skip':>5} {'preFTP':>7} "
        f"{'ord.med':>8} {'ord.min':>9} {'ord.max':>9} "
        f"{'corrupt':>8} {'suspect':>8} {'dCTL':>8} {'dATL':>8} {'dTSB':>8}"
    )
    for entry in result["per_user"]:
        pop = entry["populations"]["ordinary"]["tss_delta"]
        print(
            f"  {entry['user_id']:>4} {entry['rows_seen']:>6} "
            f"{entry['rows_changed']:>6} {entry['rows_skipped']:>5} "
            f"{entry['pre_history_rows']:>7} "
            f"{_fmt(pop['median']):>8} {_fmt(pop['min']):>9} "
            f"{_fmt(pop['max']):>9} "
            f"{entry['populations']['corrupt']['rows']:>8} "
            f"{entry['populations']['suspect']['rows']:>8} "
            f"{_fmt(entry['load_shift']['ctl'], 2):>8} "
            f"{_fmt(entry['load_shift']['atl'], 2):>8} "
            f"{_fmt(entry['load_shift']['tsb'], 2):>8}"
        )
    print()
    print("Today's load numbers, before and after (same exclusions):")
    print(
        f"  {'uid':>4} {'CTL':>16} {'ATL':>16} {'TSB':>16}"
    )
    for entry in result["per_user"]:
        before, after = entry["load_before"], entry["load_after"]
        cells = " ".join(
            f"{before[key]:7.1f}->{after[key]:7.1f}"
            for key in ("ctl", "atl", "tsb")
        )
        print(f"  {entry['user_id']:>4} {cells}")
    print()
    print(
        "What each user's rows were previously scored against, back-solved "
        "from np/if_ (top 5 clusters, rounded):"
    )
    for entry in result["per_user"]:
        clusters = ", ".join(
            f"{item['watts']}W x{item['rows']}"
            for item in entry["basis_clusters"][:5]
        )
        first = entry["first_ftp_date"] or "none"
        print(f"  {entry['user_id']:>4} (first ftp_history {first}): {clusters}")
    print()
    if result["skips"]:
        print(f"WARNING: {totals['rows_skipped']} rows were skipped and keep "
              "their current values beside rescored neighbours:")
        for skip in result["skips"]:
            ids = ", ".join(str(i) for i in skip["ids"][:10])
            more = "" if len(skip["ids"]) <= 10 else f", ... (+{len(skip['ids']) - 10})"
            print(
                f"  user {skip['user_id']}: {len(skip['ids'])} row(s) - "
                f"{skip['reason']} [ids {ids}{more}]"
            )
        print()
    if result["manual_corrections"]:
        print(
            f"{len(result['manual_corrections'])} row(s) carry a manual power "
            "correction. Power is preserved; IF and TSS are rebased:"
        )
        for row in result["manual_corrections"]:
            print(
                f"  user {row['user_id']} activity {row['id']} "
                f"({row['start_time']}): TSS {row['old_tss']:.1f} -> "
                f"{row['new_tss']:.1f}, IF {row['old_if']:.3f} -> "
                f"{row['new_if']:.3f}"
            )
        print()
    if result["backup"]:
        print(f"Backup: {result['backup']}")
    if result["interrupted"]:
        print("Checkpoint saved; run the same command again to resume.")
    elif result["write"]:
        print("Complete; progress file cleared.")


# --- the pass ----------------------------------------------------------------

def _accumulate(
    stats: Dict[str, _Stats], population: str, *, changed: bool,
    delta: float, pre_history: bool,
) -> None:
    entry = stats[population]
    entry.rows += 1
    if pre_history:
        entry.pre_history += 1
    if changed:
        entry.changed += 1
        entry.deltas.append(delta)
    else:
        entry.unchanged += 1


def _merge(into: Dict[str, _Stats], other: Dict[str, _Stats]) -> None:
    for name, entry in other.items():
        target = into[name]
        target.rows += entry.rows
        target.changed += entry.changed
        target.unchanged += entry.unchanged
        target.pre_history += entry.pre_history
        target.deltas.extend(entry.deltas)


def _new_stats() -> Dict[str, _Stats]:
    return {name: _Stats() for name in POPULATIONS}


def _reject_provenance_dependent_bases(source: str, read_only: bool) -> None:
    """Refuse to run if any stored FTP would make the scoring rail query elsewhere.

    ``is_plausible_ftp`` decides a basis between ``FTP_ASSERTION_MIN_WATTS`` and
    ``FTP_PLAUSIBLE_MIN_WATTS`` by looking up provenance in the *default*
    database with a writable connection. That is fine on the live import path
    and wrong here twice over: this tool is usually pointed at a copy, and a
    dry run must not open the live database at all, let alone for writing. No
    such value exists in this database today, so refusing outright costs
    nothing and keeps the guarantee absolute.
    """
    conn = db.connect(source, read_only=read_only)
    try:
        rows = conn.execute(
            "SELECT DISTINCT ftp_watts FROM ftp_history "
            "WHERE ftp_watts >= ? AND ftp_watts < ?",
            (FTP_ASSERTION_MIN_WATTS, FTP_PLAUSIBLE_MIN_WATTS),
        ).fetchall()
    finally:
        conn.close()
    if rows:
        values = ", ".join(f"{float(row[0]):g}" for row in rows)
        raise RuntimeError(
            f"ftp_history in {source} contains sub-floor wattages ({values}) "
            "whose admissibility is resolved against the default database. "
            "Refusing to run rather than read a database other than --db."
        )


def run(
    source: str,
    *,
    write: bool = False,
    state_path: Optional[str] = None,
    backup_path: Optional[str] = None,
    chunk_size: int = 500,
    stop_after_chunks: Optional[int] = None,
) -> dict:
    """Run a dry report or a resumable repair against ``source``.

    ``stop_after_chunks`` is a test/integration hook that simulates an
    interrupted process after a committed checkpoint; it is not exposed by the
    command-line interface.
    """
    source = os.path.abspath(source)
    if not os.path.isfile(source):
        raise ValueError(f"database not found: {source}")
    if chunk_size <= 0:
        raise ValueError("chunk size must be positive")
    if stop_after_chunks is not None and stop_after_chunks <= 0:
        raise ValueError("stop_after_chunks must be positive")

    _reject_provenance_dependent_bases(source, read_only=not write)

    state_file = os.path.abspath(state_path or f"{source}.ftp-rescore.json")
    backup_file = None
    state = None
    if write:
        destination = os.path.abspath(
            backup_path or f"{source}.before-ftp-rescore.db"
        )
        state = _load_state(state_file, source)
        if state is None:
            backup_file = _create_backup(source, destination)
            state = {
                "version": _STATE_VERSION,
                "db": source,
                "backup": backup_file,
                "backup_sha256": _file_digest(backup_file),
                "users": {},
            }
            _save_state(state_file, state)
        else:
            backup_file = _verify_recorded_backup(state, destination)

    read_only = not write
    users = db.user_ids(source, read_only=read_only)
    first_ftp = _first_ftp_dates(source, users, read_only)

    totals = _new_stats()
    per_user: List[dict] = []
    skips: List[dict] = []
    manual_corrections: List[dict] = []
    total_shift = {"ctl": 0.0, "atl": 0.0, "tsb": 0.0}
    rows_seen = 0
    rows_skipped = 0
    chunks = 0
    interrupted = False

    for uid in users:
        stats = _new_stats()
        basis_clusters: Dict[int, int] = {}
        skipped_no_date: List[int] = []
        skipped_unscoreable: List[int] = []
        daily_before = db.daily_tss(uid, source, read_only=read_only)
        daily_after = dict(daily_before)
        user_rows = 0
        after_id = int(state["users"].get(str(uid), 0)) if state else 0
        boundary = first_ftp.get(uid)

        while not interrupted:
            ids = db.activity_ids_after(
                uid, after_id, chunk_size, source, read_only=read_only
            )
            if not ids:
                break
            updates = []
            returned = set()
            for rows in db.activities_for_ftp_rescore(
                uid, ids, source, include_streams=True, read_only=read_only
            ):
                for row in rows:
                    returned.add(int(row["id"]))
                    scored = score_activity(row)
                    day = _activity_day(row)
                    old_tss = _number(row.get("tss"))
                    if scored is None:
                        skipped_unscoreable.append(int(row["id"]))
                        continue
                    user_rows += 1
                    population = classify(row)
                    basis = implied_basis(row)
                    if basis is not None:
                        key = int(round(basis))
                        basis_clusters[key] = basis_clusters.get(key, 0) + 1
                    new_tss = float(scored["tss"])
                    changed = (
                        row.get("if_") != scored["if_"]
                        or row.get("tss") != scored["tss"]
                    )
                    _accumulate(
                        stats,
                        population,
                        changed=changed,
                        delta=new_tss - old_tss,
                        pre_history=(
                            boundary is not None and day is not None
                            and day < boundary
                        ),
                    )
                    if changed:
                        updates.append(scored)
                    if scored["has_correction"]:
                        manual_corrections.append({
                            "user_id": uid,
                            "id": scored["id"],
                            "start_time": row.get("start_time"),
                            "old_tss": old_tss,
                            "new_tss": new_tss,
                            "old_if": _number(row.get("if_")),
                            "new_if": float(scored["if_"]),
                        })
                    if row.get("duplicate_of") is None and day is not None:
                        if population in ("corrupt", "suspect"):
                            # Excluded from BOTH sides: a 1.6e7 TSS row makes
                            # every load number for that user meaningless, and
                            # keeping it on the "before" side would report a
                            # shift of tens of thousands of CTL that says
                            # nothing about what the repair does to real rides.
                            # Dropping it from both leaves a shift computed
                            # only over rides whose stored value was sane.
                            daily_before[day] = daily_before.get(day, 0.0) - old_tss
                            daily_after[day] = daily_after.get(day, 0.0) - old_tss
                        else:
                            daily_after[day] = (
                                daily_after.get(day, 0.0) - old_tss + new_tss
                            )
            skipped_no_date.extend(i for i in ids if i not in returned)
            if write and updates:
                db.update_activity_ftp_metrics(uid, updates, source)
            after_id = ids[-1]
            chunks += 1
            if write:
                state["users"][str(uid)] = after_id
                _save_state(state_file, state)
            if stop_after_chunks is not None and chunks >= stop_after_chunks:
                interrupted = True

        before_load = _load_totals(daily_before)
        after_load = _load_totals(daily_after)
        shift = {key: after_load[key] - before_load[key] for key in total_shift}
        for key in total_shift:
            total_shift[key] += shift[key]
        for reason, ids_ in (
            (_SKIP_NO_DATE, skipped_no_date),
            (_SKIP_NOT_SCOREABLE, skipped_unscoreable),
        ):
            if ids_:
                skips.append(
                    {"user_id": uid, "reason": reason, "ids": sorted(ids_)}
                )
                rows_skipped += len(ids_)
        rows_seen += user_rows
        _merge(totals, stats)
        per_user.append({
            "user_id": uid,
            "rows_seen": user_rows,
            "rows_changed": sum(entry.changed for entry in stats.values()),
            "rows_unchanged": sum(entry.unchanged for entry in stats.values()),
            "rows_skipped": len(skipped_no_date) + len(skipped_unscoreable),
            "pre_history_rows": sum(entry.pre_history for entry in stats.values()),
            "first_ftp_date": boundary.isoformat() if boundary else None,
            "basis_clusters": sorted(
                ({"watts": w, "rows": n} for w, n in basis_clusters.items()),
                key=lambda item: (-item["rows"], item["watts"]),
            ),
            "populations": {n: s.summary() for n, s in stats.items()},
            "load_before": before_load,
            "load_after": after_load,
            "load_shift": shift,
        })
        if interrupted:
            break

    if write and not interrupted:
        _clear_state(state_file)

    pre_history_rows = sum(entry.pre_history for entry in totals.values())
    result = {
        "db": source,
        "write": write,
        "totals": {
            "rows_seen": rows_seen,
            "rows_changed": sum(entry.changed for entry in totals.values()),
            "rows_unchanged": sum(entry.unchanged for entry in totals.values()),
            "rows_skipped": rows_skipped,
            "pre_history_rows": pre_history_rows,
            "pre_history_pct": (
                100.0 * pre_history_rows / rows_seen if rows_seen else 0.0
            ),
        },
        "populations": {n: s.summary() for n, s in totals.items()},
        "per_user": per_user,
        "skips": skips,
        "manual_corrections": manual_corrections,
        "load_shift": total_shift,
        "backup": backup_file,
        "interrupted": interrupted,
    }
    return result


def _number(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _activity_day(row: dict) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(str(row.get("start_time") or "")[:10])
    except ValueError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report or repair stored activity TSS using FTP history."
    )
    parser.add_argument("--db", default=db_path(), help="SQLite database path")
    parser.add_argument(
        "--write", action="store_true", help="write the repair after making a backup"
    )
    parser.add_argument("--state", help="progress JSON path for --write")
    parser.add_argument("--backup", help="verified SQLite backup path for --write")
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--json", help="also write the full report as JSON here")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = run(
            args.db,
            write=args.write,
            state_path=args.state,
            backup_path=args.backup,
            chunk_size=args.chunk_size,
        )
        _print_report(result)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2, sort_keys=True)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
