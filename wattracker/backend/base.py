"""The seam between the app and *the user's machine*.

Everything wattracker does is machine-agnostic except three things: it reads
``.fit`` files out of the Zwift Activities folder, writes ``.zwo`` files into
the Zwift Workouts folder, and talks BLE to the trainer. Those three are the
only reason the app has to run on the same box as Zwift.

This module names that boundary so a second implementation can put it on the
other end of a network link (see ``remote.py``), while the default one
(``local.py``) keeps doing exactly what the app has always done: call
``paths``/``zwo`` directly, on this machine.

Nothing here knows about the database or a user's settings - callers resolve
those and pass the results in. That keeps a backend implementable by a small
connector process that has no database at all.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import ContextManager, List, Optional, Tuple


class BackendUnavailable(RuntimeError):
    """The machine this backend speaks for cannot be reached right now.

    Only the remote backend raises it (no connector attached, or it dropped
    mid-call). Callers should degrade to a clear message rather than a 500 -
    the browser-download routes remain a working fallback for exports.
    """


@dataclass(frozen=True)
class ActivityFile:
    """One candidate ``.fit`` file on the user's machine.

    ``path`` is the path *as that machine sees it*, which is what keys the
    ``scanned_files`` table - so the incremental-rescan cache keeps working
    unchanged when the file lives on a different host.
    """

    path: str
    name: str
    mtime: float
    size: int


@dataclass(frozen=True)
class ActivityListing:
    """The result of looking for activity files.

    ``directory`` is echoed back because in remote mode the server does not
    know which folder the connector resolved, and ``scan_activities`` reports
    it. ``exists`` distinguishes "no Zwift folder here" from "folder is empty".

    ``skipped`` counts files that are present but deliberately not offered -
    Zwift's in-progress recording buffer, and anything that vanished or turned
    unreadable between listing and stat. They are filtered on the machine that
    owns them (so a remote backend never ships them over the wire), but the
    scan still reports them, so the count has to travel separately.
    """

    directory: Optional[str]
    exists: bool
    files: List[ActivityFile] = field(default_factory=list)
    skipped: int = 0


@dataclass(frozen=True)
class ExportManifest:
    """The desired state of a user's Zwift custom-workout folder.

    Computed entirely from the database (see ``exporter.plan_export_manifest``)
    and then handed to a backend to apply. ``write`` entries carry the shape
    ``zwo.write_plan_to_zwift`` already expects: ``{"date", "name", "zwo"}``.
    ``remove`` carries bare filenames from ``zwo.plan_filename``.

    ``require_existing`` preserves a real behavioural difference between the
    two callers: the plan-export sweep creates the target folder if needed,
    while the adapt/reflow re-export path deliberately does nothing when the
    folder is absent (it is a best-effort tidy-up, not a user action).

    ``resolution`` preserves another, less defensible one. The sweep resolves
    its target with ``resolve_export_dir``, which refuses to guess between
    several Zwift player folders; the explicit per-plan and per-workout export
    buttons instead go straight through ``workouts_dir``, which always yields
    a path and creates it. So with more than one player folder those two
    actions can write to different places. That is pre-existing behaviour, kept
    verbatim here rather than quietly changed - worth reconciling separately.
    """

    zwift_id: str
    override: Optional[str] = None
    write: List[dict] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    require_existing: bool = False
    resolution: str = "resolve"  # 'resolve' | 'direct'


class Backend(abc.ABC):
    """Access to the Zwift-side of one user's machine."""

    #: Short identifier used in logs and surfaced in Settings.
    name: str = "backend"

    # ----------------------------------------------------- discovery

    @abc.abstractmethod
    def activity_candidates(self) -> List[dict]:
        """``[{"path": str, "exists": bool}]`` for the Activities folder UI."""

    @abc.abstractmethod
    def zwift_id_candidates(self) -> List[dict]:
        """``[{"zwift_id": str, "path": str, "mtime": float}]``, newest first."""

    @abc.abstractmethod
    def default_activities_dir(self) -> Optional[str]:
        """Best-guess Activities folder with no per-user override applied."""

    @abc.abstractmethod
    def workouts_root(self) -> Optional[str]:
        """The Zwift ``Workouts`` root (the parent of the player-id folders)."""

    @abc.abstractmethod
    def resolve_export_dir(
        self, zwift_id: Optional[str] = None, override: Optional[str] = None
    ) -> Tuple[Optional[str], str]:
        """``(directory, reason)``; reason is one of
        ``override|zwift_id|detected|choose|missing``."""

    @abc.abstractmethod
    def validate_dir(self, value: str) -> Tuple[Optional[str], Optional[str]]:
        """Validate a user-submitted folder path against the trusted roots.

        Returns ``(clean_path, error)``. An empty submission means "unchanged"
        and yields ``("", None)``. This must run on the machine that owns the
        path - validating a client path against the server's roots would be
        meaningless.
        """

    # ----------------------------------------------- activity files

    @abc.abstractmethod
    def list_activities(self, directory: Optional[str] = None) -> ActivityListing:
        """List ``.fit`` files in ``directory`` (or the user's default)."""

    @abc.abstractmethod
    def readable_activity(self, path: str) -> "ContextManager[str]":
        """A context manager yielding a *local filesystem path* to parse.

        Local backends yield the path itself, so scanning stays a plain read
        with no copy - which matters, the parser is the expensive part of a
        rescan over hundreds of files. Remote backends fetch the bytes into a
        temporary file and clean it up on exit.
        """

    # ------------------------------------------------ workout files

    @abc.abstractmethod
    def apply_exports(self, manifest: ExportManifest) -> dict:
        """Write and prune ``.zwo`` files to match ``manifest``.

        Returns ``{"status", "directory", "exported", "removed", "reason",
        "paths"}`` where status is ``ok`` or one of the unresolvable
        ``resolve_export_dir`` reasons (``choose``/``missing``). ``paths`` are
        as the owning machine sees them, which is what the UI should show the
        rider - they go looking for the file in Zwift, not on the server.
        """
