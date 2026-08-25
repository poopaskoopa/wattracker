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
saving is imaginary. The interval also *is* the settle window: Zwift writes a
.fit over some seconds, and a file is reported only once its size has stopped
changing between two passes. An event API would deliver the first write
immediately and leave the debounce to be built by hand.

**Change-gated, not a heartbeat.** A timer that simply told the server "rescan
now" every minute would move the clock across the wire and change nothing: the
server would still make an ``activities.list`` round trip per tick. Reporting
only differences means an idle connector costs nothing at all, and a finished
ride costs exactly one frame.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

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
# should be a busy folder read, not a busy loop.
MIN_INTERVAL_S = 5.0

# What a file looks like to us. Mtime and size together, which is the same
# pair the server's own ``scanned_files`` cache is keyed on, so the two agree
# about what "unchanged" means.
_Reading = Dict[str, Tuple[float, int]]


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

    def __init__(self, config: ConnectorConfig) -> None:
        self._config = config
        # The previous pass, used only to decide whether a file has stopped
        # changing. None until the first pass has run.
        self._previous: Optional[_Reading] = None
        # Everything the server has already been told about. Kept separately
        # from ``_previous`` because they answer different questions: one is
        # "did this file settle?", the other "is this news?".
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

        Afterwards a file is reported only once it has stopped changing: it
        must read identically in two consecutive passes before it counts. A
        .fit that Zwift is still writing therefore waits, instead of being
        offered half-written and failing to parse on the other side.

        Deletions never report. The server's scan only ever imports, so a file
        going away gives it nothing to do - but it is forgotten here, so that
        the same name appearing again is news a second time.
        """
        current = self._read()

        if self._previous is None:
            self._previous = current
            self._reported = dict(current)
            return True

        settled = {
            path: value for path, value in current.items()
            if self._previous.get(path) == value
        }
        self._previous = current

        fresh = {
            path: value for path, value in settled.items()
            if self._reported.get(path) != value
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
