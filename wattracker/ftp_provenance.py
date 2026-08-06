"""Durable provenance for an FTP: did the rider assert it, or did we estimate it?

The plausibility floor in :mod:`wattracker.metrics.power` rejects a FAILED
ESTIMATE, not a rider. Telling those apart is therefore load-bearing, and the
answer has to survive a trip through SQLite: the importer resolves an FTP in
one process, stores the resulting scores, and PR #59's ``ftp_rescore`` later
re-reads a basis straight out of ``ftp_history`` (``float(row["ftp_watts"])``)
and re-scores from it. A provenance flag that lives only on a Python object -
``AssertedFTP`` - is invisible to that second reader, which then sees a bare
40.0, calls it a failed estimate and zeroes a rider's whole training history.

So provenance is resolved from the database, which has recorded it all along:

* ``ftp_history.source`` distinguishes a rider's entry from ours, and
* ``user_settings.ftp`` is by definition the rider's own statement - it is only
  ever written when a human types a number into Settings or the setup wizard.

``AssertedFTP`` survives as an in-process convenience for the value the
importer has *just* resolved (it saves a query on the hot path and works before
the assertion has been written anywhere), but it is no longer the only way to
learn the answer: anything that reads a basis out of the database can ask here
and get the same one.

Deliberate property: the lookup is by WATTAGE, not by user. The question this
answers is "is this number a real FTP somebody stated, or estimator garbage" -
a physical question about the value, not an authorization question about a row.
Pairing a basis with the right rider is the caller's job (the rescore's SQL
already joins on ``user_id``); all that leaks across users here is whether one
unusually low wattage is admissible as a basis at all, and a wattage a human
asserted is a physically real wattage regardless of who asserted it.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

_log = logging.getLogger(__name__)

# The sources of an ``ftp_history`` row that are the RIDER's statement rather
# than our estimate. Named for what an assertion IS: the predicate used to be
# ``source != "estimated"``, which is fail-open - the first time anyone adds a
# new estimator and writes ``add_ftp_entry(..., "ramp_test")``, an unbounded
# machine-produced number would silently be honoured as a rider assertion and
# reopen issue #60. An unrecognised source is treated as an estimate, so a new
# writer has to opt in here explicitly.
ASSERTED_FTP_SOURCES = frozenset({"manual"})

# Wattages are compared as stored IEEE doubles - a basis read back out of the
# same column is bit-identical, so this is an equality test with only enough
# slack to absorb a REAL round-trip, NOT a "close enough" match. Anything
# looser is an attack surface: with a 2% tolerance, a rider who sets their FTP
# to 0.64 makes a corrupt 0.6378 W legacy basis "their assertion" and re-scores
# a ride at TSS 1,633,482.
_EQUAL_ABS_TOL = 1e-9


def is_asserted_source(source) -> bool:
    """Whether an ``ftp_history.source`` marks the row as a rider assertion."""
    if not isinstance(source, str):
        return False
    return source.strip().lower() in ASSERTED_FTP_SOURCES


def is_asserted_watts(watts, path: Optional[str] = None) -> bool:
    """Whether the database records ``watts`` as a wattage a rider asserted.

    True when some ``ftp_history`` row with an asserted source, or some stored
    ``user_settings.ftp``, holds this exact value. Any failure to answer -
    missing database, missing tables, a locked file - is reported as False:
    this gates whether an *implausibly low* number may be used as a scoring
    basis, and the safe answer when we cannot tell is "no".
    """
    try:
        value = float(watts)
    except (TypeError, ValueError, OverflowError):
        return False
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return False
    placeholders = ",".join("?" for _ in ASSERTED_FTP_SOURCES)
    sources = sorted(ASSERTED_FTP_SOURCES)
    try:
        from . import db

        conn = db.connect(path)
    except Exception:  # pragma: no cover - defensive: never fail a scorer
        _log.debug("could not open the database to resolve FTP provenance",
                   exc_info=True)
        return False
    try:
        row = conn.execute(
            f"""
            SELECT 1 FROM ftp_history
             WHERE LOWER(TRIM(source)) IN ({placeholders})
               AND ABS(ftp_watts - ?) <= ?
            UNION ALL
            SELECT 1 FROM user_settings
             WHERE ftp IS NOT NULL AND ABS(ftp - ?) <= ?
            LIMIT 1
            """,
            (*sources, value, _EQUAL_ABS_TOL, value, _EQUAL_ABS_TOL),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        _log.debug("could not resolve FTP provenance for %r", watts, exc_info=True)
        return False
    finally:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
