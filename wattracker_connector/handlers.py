"""What a connector can be asked to do.

Every entry in ``build_handlers()`` is one RPC method the server may call. They
are plain async functions over the local filesystem, with no knowledge of
sockets or reconnection - which is what makes them straightforward to test
against a temporary directory pretending to be a Zwift install.

The mirror-image of ``wattracker/backend/local.py``: that one runs these same
operations in-process, this one runs them across a wire. Keeping the two in
step is the job of tests/test_backend_parity.py.
"""
from __future__ import annotations

import base64
import glob
import logging
import os
import re
from typing import Callable, Dict, List, Optional

from wattracker import paths
from wattracker.prescribe import zwo

log = logging.getLogger(__name__)

# Must not exceed the server's MAX_UPLOAD_BYTES (50 MiB): a file the server
# would refuse should not be read into memory here either.
MAX_ACTIVITY_BYTES = 50 * 1024 * 1024

# Zwift's live recording buffer. Never a finished ride, and its start second
# collides with the eventual final .fit - filtered here so it is neither
# transferred nor cached server-side.
_IN_PROGRESS = "inprogressactivity.fit"

# A plan workout's date, as it leads the exported .zwo filename. Held here as
# well as in zwo.plan_filename because this end judges the manifest before
# acting on it, rather than relying on the far end having done so.
_ISO_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class ConnectorConfig:
    """The bits of local configuration the handlers need.

    Folder overrides come from the connector's own config file, not from the
    server: they describe this machine, and the person who set them is sitting
    at it.
    """

    def __init__(
        self,
        activities_dir: Optional[str] = None,
        workouts_dir: Optional[str] = None,
    ) -> None:
        self.activities_dir = activities_dir or None
        self.workouts_dir = workouts_dir or None


def _resolved_activities_dir(config: ConnectorConfig,
                             directory: Optional[str]) -> Optional[str]:
    """The folder to work in: server's suggestion, local override, discovery.

    A folder the *server* names has to clear this machine's trusted roots
    before we will act on it; one this machine configured or discovered for
    itself is trusted already, because the person who set it was sitting here.
    Returns ``None`` when the server asked for somewhere it may not go, which
    every caller treats as "no such folder" rather than an error worth
    explaining to a caller that should not have asked.

    Listing and reading must resolve through *this* function identically. They
    did not always: ``activities.read`` used to ignore ``directory`` and check
    against the local folder alone, so whenever the server carried an
    ``activities_dir`` override that differed from the connector's own - which
    is exactly what setting one in the web UI does - the listing succeeded and
    then every single read of the files it had just offered was refused.
    """
    local = config.activities_dir or paths.activities_dir()
    if not directory:
        return local
    if local and _within(local, directory) and _within(directory, local):
        return local  # the connector's own folder, handed straight back
    clean, error = validate_dir(directory, require_exists=False)
    if clean is None:
        log.warning("refusing server-supplied activities folder: %s", error)
    return clean


def _within(directory: str, candidate: str) -> bool:
    """Whether ``candidate`` really sits inside ``directory``.

    Symlinks resolved first, so a link planted in the Activities folder cannot
    be used to make the connector read an arbitrary file off this machine and
    ship it to the server.
    """
    try:
        root = os.path.realpath(os.path.abspath(directory))
        target = os.path.realpath(os.path.abspath(candidate))
        return os.path.commonpath([root, target]) == root
    except (ValueError, OSError):
        return False  # different Windows drives, or an unresolvable path


def _is_activity_file(name: str) -> bool:
    """Whether a filename is one this connector will ever hand over.

    A ``.fit``, and never Zwift's live recording buffer. It used to live only
    inside ``activities.list``'s glob, which meant the read path enforced no
    such thing: it checked containment and nothing else, so any file at all
    under a folder the server named came back - an ssh key, or this
    connector's own config file with the device token in it.
    """
    lowered = name.lower()
    return lowered.endswith(".fit") and lowered != _IN_PROGRESS


def _in_scope(directory: str, path: str) -> bool:
    """Whether ``path`` is an activity file this connector will serve.

    Applied by the listing AND the read, which is the point: the read used to
    test containment alone, and containment is not the rule. The listing is a
    non-recursive glob for ``.fit``, so being *somewhere under* the folder is
    no evidence a file was ever on offer - it has to sit directly in it and be
    an activity file.

    Symlinks are resolved before both tests, so a ``.fit`` planted in the
    Activities folder cannot stand in for something else. That does mean a
    rider whose Activities folder is full of links to files kept elsewhere gets
    nothing listed; they should point the connector at where the files actually
    are. Visibly skipping such a file is the better failure - the alternative
    is a listing and a read that disagree, which is a scan that re-offers the
    same file and fails on it forever.
    """
    try:
        root = os.path.realpath(os.path.abspath(directory))
        resolved = os.path.realpath(os.path.abspath(path))
    except (ValueError, OSError):
        return False  # different Windows drives, or an unresolvable path
    return (
        os.path.dirname(resolved) == root
        and _is_activity_file(os.path.basename(resolved))
    )


def build_handlers(config: ConnectorConfig) -> Dict[str, Callable]:
    """The method table this connector answers to."""

    # ------------------------------------------------------- discovery
    async def paths_activity_candidates() -> List[dict]:
        return paths.annotated_candidates()

    async def paths_zwift_id_candidates() -> List[dict]:
        return paths.candidate_zwift_ids()

    async def paths_default_activities_dir() -> Optional[str]:
        return config.activities_dir or paths.activities_dir()

    async def paths_workouts_root() -> Optional[str]:
        return paths.zwift_workouts_root()

    async def paths_resolve_export_dir(
        zwift_id: Optional[str] = None, override: Optional[str] = None
    ) -> dict:
        directory, reason = paths.resolve_export_dir(
            zwift_id, override or config.workouts_dir
        )
        return {"directory": directory, "reason": reason}

    async def paths_validate_dir(
        value: str = "", require_exists: bool = True
    ) -> dict:
        clean, error = validate_dir(value, require_exists=require_exists)
        return {"clean": clean, "error": error}

    # -------------------------------------------------- activity files
    async def activities_list(directory: Optional[str] = None) -> dict:
        target = _resolved_activities_dir(config, directory)
        if not target or not os.path.isdir(target):
            return {"directory": target, "exists": False, "files": [], "skipped": 0}

        found: List[str] = []
        for pattern in ("*.fit", "*.FIT"):
            found.extend(glob.glob(os.path.join(target, pattern)))

        files: List[dict] = []
        skipped = 0
        for path in sorted(set(found)):
            name = os.path.basename(path)
            if not _in_scope(target, path):
                skipped += 1
                continue
            try:
                st = os.stat(path)
            except OSError:
                skipped += 1
                continue
            files.append(
                {"path": path, "name": name,
                 "mtime": st.st_mtime, "size": st.st_size}
            )
        return {"directory": target, "exists": True,
                "files": files, "skipped": skipped}

    async def activities_read(path: str = "",
                              directory: Optional[str] = None) -> dict:
        """Return one activity file's bytes, base64-encoded.

        Confined to files the listing offered: an activity file sitting
        directly in the resolved Activities folder, and nothing else. The
        server chooses this path from a listing this connector produced, so
        anything else means either a bug or a server that should not be trusted
        with arbitrary local reads - either way, refuse. ``directory`` is
        resolved by exactly the rule the listing used and the file is judged by
        exactly the predicate the listing used (``_in_scope``), so the two
        cannot disagree about what is in scope.

        The weaker check this replaces was containment alone, which made this a
        general file-read primitive over everything the server could name under
        this machine's trusted roots - ``~/.ssh/id_ed25519`` and this
        connector's own token file included.
        """
        target = _resolved_activities_dir(config, directory)
        if not target or not _in_scope(target, path):
            raise ValueError("path is outside the activities folder")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such activity file: {path}")
        size = os.path.getsize(path)
        if size > MAX_ACTIVITY_BYTES:
            raise ValueError(
                f"{os.path.basename(path)} is {size} bytes, over the "
                f"{MAX_ACTIVITY_BYTES} limit"
            )
        with open(path, "rb") as handle:
            content = handle.read()
        return {
            "path": path,
            "name": os.path.basename(path),
            "content": base64.b64encode(content).decode("ascii"),
        }

    # --------------------------------------------------- workout files
    async def workouts_sync(
        zwift_id: Optional[str] = None,
        override: Optional[str] = None,
        write: Optional[List[dict]] = None,
        remove: Optional[List[str]] = None,
        require_existing: bool = False,
        resolution: str = "resolve",
    ) -> dict:
        """Make the Zwift custom-workout folder match the server's manifest."""
        effective_override = override or config.workouts_dir
        if resolution == "direct":
            try:
                target = paths.workouts_dir(zwift_id, override=effective_override)
            except paths.ExportTargetUnavailable as exc:
                return {"status": exc.reason, "directory": None, "exported": 0,
                        "removed": 0, "reason": exc.reason, "paths": []}
            reason = "override" if effective_override else "direct"
        else:
            target, reason = paths.resolve_export_dir(zwift_id, effective_override)
        if not target:
            return {"status": reason, "directory": None, "exported": 0,
                    "removed": 0, "reason": reason, "paths": []}
        if require_existing and not os.path.isdir(target):
            return {"status": "missing", "directory": None, "exported": 0,
                    "removed": 0, "reason": "missing", "paths": []}

        written: List[str] = []
        # Same principle as the ``remove`` guard below, applied to the side
        # that creates files rather than deletes them: the server derives each
        # date from a plan row, but this end must not take that on trust. The
        # date leads the filename, so a value like "/etc/cron.d/x" or
        # "../../.config/autostart/pwn" made this an arbitrary-path write.
        # zwo.plan_filename now sanitises it too - this refuses rather than
        # quietly writing the mangled name, because a manifest entry that is
        # not a date is not a workout anybody asked to export.
        writable: List[dict] = []
        for entry in write or []:
            if not isinstance(entry, dict):
                log.warning("refusing a workout that is not an object")
            elif not _ISO_DATE_RE.match(str(entry.get("date") or "")):
                log.warning("refusing a workout with a suspicious date %r",
                            entry.get("date"))
            else:
                writable.append(entry)

        if writable:
            try:
                result = zwo.write_plan_to_zwift(
                    writable, zwift_id, workouts_override=effective_override
                )
            except paths.ExportTargetUnavailable as exc:
                return {"status": exc.reason, "directory": None, "exported": 0,
                        "removed": 0, "reason": exc.reason, "paths": []}
            written = result["paths"]

        removed = 0
        for filename in remove or []:
            # Filenames come from zwo.plan_filename server-side, but this end
            # must not take that on trust: a name with a separator in it would
            # delete outside the Zwift folder.
            if os.path.basename(filename) != filename:
                log.warning("refusing to remove suspicious filename %r", filename)
                continue
            candidate = os.path.join(target, filename)
            try:
                if os.path.exists(candidate):
                    os.unlink(candidate)
                    removed += 1
            except OSError as exc:
                log.warning("could not remove export %s: %s", candidate, exc)

        return {"status": "ok", "directory": target, "exported": len(written),
                "removed": removed, "reason": reason, "paths": written}

    return {
        "paths.activity_candidates": paths_activity_candidates,
        "paths.zwift_id_candidates": paths_zwift_id_candidates,
        "paths.default_activities_dir": paths_default_activities_dir,
        "paths.workouts_root": paths_workouts_root,
        "paths.resolve_export_dir": paths_resolve_export_dir,
        "paths.validate_dir": paths_validate_dir,
        "activities.list": activities_list,
        "activities.read": activities_read,
        "workouts.sync": workouts_sync,
    }


def validate_dir(
    value: str, require_exists: bool = True
) -> "tuple[Optional[str], Optional[str]]":
    """Validate a user-supplied folder against this machine's trusted roots.

    The same check the single-machine install runs, because it IS the same
    check - ``paths.confine_storage_dir``, the one place the rule for a
    SUBMITTED path lives. It is invoked from here rather than from the server
    because in a server/client install the folders being judged are *these*,
    and the server's home directory has no bearing on them; the rule does not
    change with the machine, only the roots it is measured against do.
    """
    return paths.confine_storage_dir(value, must_exist=require_exists)
