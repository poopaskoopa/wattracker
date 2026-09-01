"""Notices when a finished ride lands in the Zwift Activities folder.

The server owns the database and therefore owns the decision to import, but it
cannot see the files: they live on this machine, behind the same home router
the connector dials out through. So the server used to ask on a timer - once a
day - and an offline connector at that moment cost a full day. Worse, in the
normal case the connector is a tray icon that never disconnects at all, so
"scan when a connector attaches" would fire once at boot, hours before the
ride existed.

This module closes that gap from the only end that can see it. It watches the
folders ``activities_scope`` already serves and reports *changes*, which the
client turns into one ``activities.changed`` event. The server then runs the
scan it would have run anyway.

Two decisions worth stating, because both look like the lazy option and
neither is:

**Polling, not filesystem events.** ``watchdog`` would be the obvious
dependency, and this package deliberately has almost none - it freezes into a
small executable and every import is weight in it (see ``rpc``'s note on
staying stdlib-only). A ``scandir`` of one folder costs microseconds, so the
saving is imaginary. An event API would deliver the first write immediately
and leave the debounce to be built by hand, and the debounce is the hard
part: Zwift creates the *finished* file's name at ride start and appends to
it for the whole ride, so "this file stopped changing" and "this ride ended"
are different statements. ``SETTLE_WINDOW_S`` and ``fit_is_complete`` are how
they are told apart.

**Change-gated, not a heartbeat.** A timer that simply told the server "rescan
now" every minute would move the clock across the wire and change nothing: the
server would still make an ``activities.list`` round trip per tick. Reporting
only differences means an idle connector costs nothing at all, and a finished
ride costs exactly one frame.

One consequence of both decisions, stated here so it is not mistaken for a
bug: the cold-start report is not instant. ``poll`` reports unconditionally on
its first pass, but the loop that calls it sleeps *before* polling, so a
connector that has just started tells the server nothing for one whole
interval - a minute by default. Worse, a connector crash-looping faster than
that never completes a first poll at all, and so never reports anything; the
rides it should have announced wait for the server's daily sweep. That is the
intended fallback rather than a second mechanism here, because a connector
that cannot stay up for a minute has a problem this module cannot fix.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Tuple

from .handlers import (
    ConnectorConfig,
    _in_scope,
    activities_scope,
    is_activity_file,
)

log = logging.getLogger(__name__)

# Seconds between passes. Deliberately unhurried: the thing being waited for is
# a ride that has already finished, nobody is watching the folder, and the cost
# of being late is measured against a sweep that used to take 24 hours. A rider
# who wants it snappier can say so (--scan-interval).
DEFAULT_INTERVAL_S = 60.0

# The floor a configured interval is held to. Not a performance guard - the
# poll is far too cheap for that - but a typo guard: ``--scan-interval 0.01``
# should be a busy folder read, not a busy loop. It no longer decides when a
# file counts as finished - SETTLE_WINDOW_S does - so a short interval only
# makes a genuine answer arrive sooner.
MIN_INTERVAL_S = 5.0

# How long a file must sit completely still before it can be reported. Wall
# clock, deliberately: the rule used to be "identical in two consecutive
# passes", which meant whatever the interval happened to be - two minutes at
# the default, ten seconds at the floor above.
#
# Ten seconds is not stillness. Measured on hardware 2026-09-01, Zwift flushes
# a ride to the final file in 4096-byte blocks 35-40s apart, so at
# --scan-interval 5 every single flush "settled" and every one asked the
# server to import a truncated file - five useless scans per ride, each one
# spending the server's own once-a-minute slot so that the real report at the
# end arrived rate-limited.
SETTLE_WINDOW_S = 60.0

# What a file looks like to us. Mtime and size together, which is the same
# pair the server's own ``scanned_files`` cache is keyed on, so the two agree
# about what "unchanged" means.
_Reading = Dict[str, Tuple[float, int]]


def fit_is_complete(path: str) -> bool:
    """Whether the file on disk is a whole FIT, rather than one being written.

    The structural half of the settle rule, and the half that does not depend
    on timing at all. A FIT starts with a header carrying the byte length of
    the data that follows it; an encoder writes that field as a placeholder,
    streams the records, then seeks back to fill it in and appends the
    two-byte CRC when the ride is saved. A file mid-ride therefore disagrees
    with its own header, and a saved one agrees with it exactly. Confirmed
    against this machine's own folders: all 221 finished Zwift rides match to
    the byte, and the one file refused is a ride Zwift abandoned - 1102 bytes
    carrying data_size 0, which is the shape of a live ride's first flush, and
    which fitdecode refuses as "not a FIT file @ 16". That file is also what
    the old rule reported on every cold start.

    FITs may be chained - several concatenated in one file - so the lengths
    are walked rather than checked once. Anything unreadable, empty, or not a
    FIT is "not complete": the server could not import it either, and the
    daily sweep stays the backstop for whatever this cannot make sense of.
    """
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            offset = 0
            while offset < size:
                handle.seek(offset)
                header = handle.read(14)
                if len(header) < 12 or header[8:12] != b".FIT":
                    return False
                header_size = header[0]
                if header_size < 12:
                    return False
                data_size = int.from_bytes(header[4:8], "little")
                # At least 14 a step, so this cannot fail to terminate.
                offset += header_size + data_size + 2
            return size > 0 and offset == size
    except OSError:
        # Being written with an exclusive handle, gone since the scandir, on a
        # disk that just went away: all the same answer, and all worth another
        # look next pass rather than a log line.
        return False


def normalize_interval(value: object) -> float:
    """Seconds between passes, or 0.0 to disable the watcher entirely.

    Accepts whatever the config file or the command line happened to hold: the
    file is editable by hand, and a string or a null in it must not stop the
    connector from starting. Anything unreadable falls back to the default
    with a warning, because silently watching nothing is the one outcome that
    would be indistinguishable from the bug this module exists to fix.
    """
    if value is None:
        return DEFAULT_INTERVAL_S
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        log.warning(
            "scan_interval %r is not a number; using the default of %.0fs",
            value, DEFAULT_INTERVAL_S,
        )
        return DEFAULT_INTERVAL_S
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        log.warning(
            "scan_interval %r is not a usable number; using the default of "
            "%.0fs", value, DEFAULT_INTERVAL_S,
        )
        return DEFAULT_INTERVAL_S
    if seconds <= 0:
        return 0.0  # explicitly off; the server's daily sweep still catches up
    if seconds < MIN_INTERVAL_S:
        log.warning(
            "scan_interval %.3fs is below the %.0fs minimum; using %.0fs",
            seconds, MIN_INTERVAL_S, MIN_INTERVAL_S,
        )
        return MIN_INTERVAL_S
    return seconds


class ActivityWatcher:
    """Remembers what the Activities folders held, and says when that changes.

    Pure and synchronous on purpose - it touches the disk and nothing else, so
    the whole settle-and-report rule can be tested by calling ``poll`` with
    files written between calls, with no event loop and no socket in the way.
    """

    def __init__(
        self,
        config: ConnectorConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        # Monotonic, so a clock correction mid-ride cannot make a file look
        # still for an hour. Injectable only so the tests can hold it.
        self._clock = clock
        # Per file, its current reading and the moment that reading was first
        # seen. That timestamp is the whole settle rule: stillness measured in
        # seconds instead of in passes.
        self._since: Dict[str, Tuple[Tuple[float, int], float]] = {}
        # Whether any pass has run at all. Not inferable from ``_since``: an
        # empty folder is a perfectly good reading.
        self._started = False
        # Everything the server has already been told about. Kept separately
        # because it answers a different question: one is "has this file
        # stopped changing?", the other "is this news?".
        self._reported: _Reading = {}

    # ------------------------------------------------------------- reading
    def folders(self) -> List[str]:
        """The folders to watch: exactly the ones the listing would serve.

        ``activities_scope`` can name the same folder twice (a configured
        override that happens to equal the OS default), and watching it twice
        would be harmless but confusing in the log.
        """
        seen = set()
        out = []
        for directory in activities_scope(self._config):
            key = os.path.normcase(os.path.abspath(directory))
            if key in seen:
                continue
            seen.add(key)
            out.append(directory)
        return out

    def _read(self) -> _Reading:
        """One pass over the folders. Missing ones are simply not there yet.

        Every failure here is expected rather than exceptional: the folder may
        not exist until Zwift has been run once, a file may vanish between the
        listing and the stat, and a rider may be mid-way through moving their
        library. None of it is worth a log line every minute, and none of it
        should stop the pass.
        """
        out: _Reading = {}
        for directory in self.folders():
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                # The same predicate the listing applies. Reused rather than
                # restated: a watcher that reported a file the listing will
                # not offer would trigger a scan that imports nothing, every
                # single pass, forever.
                #
                # Both halves of it. The name is the cheap one and only a
                # prefilter - the listing also resolves the path and requires
                # the target to sit directly in this folder, so a symlink
                # pointing out of it is precisely the file that would be
                # reported here and skipped there. handlers.py:210 documents a
                # link-filled Activities folder as supported-but-degraded, so
                # this is reachable rather than theoretical.
                if not is_activity_file(entry.name):
                    continue
                if not _in_scope(directory, entry.path):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                out[os.path.normcase(entry.path)] = (stat.st_mtime, stat.st_size)
        return out

    # ------------------------------------------------------------- polling
    def poll(self) -> bool:
        """One pass. True if there is something new worth telling the server.

        The first pass always reports. A connector that has just started may
        have been down while a ride was ridden, and it has no memory of what
        the folder held last time it ran - so the honest answer to "is there
        news?" is yes, and the scan it triggers is the cheap incremental one
        that skips everything already imported. This is what makes a connector
        restart the cold-start trigger, rather than needing a second mechanism
        on the server for it.

        Afterwards a file must pass two tests, because Zwift's ride file
        passes neither one alone. It creates the file under its FINAL name at
        ride *start* and appends to it for the whole ride, in 4096-byte blocks
        35-40s apart, so the pair (mtime, size) stands still for most of the
        ride and the old "identical in two consecutive passes" rule reported
        every flush.

        So: the reading must have been unchanged for ``SETTLE_WINDOW_S``
        seconds of wall clock - stillness that does not shrink when the rider
        asks for a faster interval - and the file must agree with its own FIT
        header, which a half-written one does not. The timing test alone
        cannot see a rider who pauses; the structural test alone trusts that
        Zwift never flushes a self-consistent file mid-ride. Together the
        server is asked to import only what it can actually parse.

        Deletions never report. The server's scan only ever imports, so a file
        going away gives it nothing to do - but it is forgotten here, so that
        the same name appearing again is news a second time.
        """
        now = self._clock()
        current = self._read()

        # Carry each file's first-seen timestamp forward for as long as its
        # reading is unchanged; a changed file starts its wait again.
        since: Dict[str, Tuple[Tuple[float, int], float]] = {}
        for path, value in current.items():
            was = self._since.get(path)
            since[path] = (value, was[1] if was and was[0] == value else now)
        self._since = since

        if not self._started:
            self._started = True
            self._reported = dict(current)
            return True

        fresh = {
            path: value for path, value in current.items()
            if now - since[path][1] >= SETTLE_WINDOW_S
            and self._reported.get(path) != value
            # Last, and only for the few files that got this far: it opens the
            # file, where everything above is arithmetic on the scandir.
            and fit_is_complete(path)
        }
        # Drop what is no longer on disk before adding what is new, so the
        # bookkeeping cannot grow without bound on a folder that is regularly
        # archived out.
        self._reported = {
            path: value for path, value in self._reported.items()
            if path in current
        }
        if not fresh:
            return False
        self._reported.update(fresh)
        log.info(
            "%d new activity file(s) settled in the Zwift folder; telling the "
            "server", len(fresh),
        )
        return True
