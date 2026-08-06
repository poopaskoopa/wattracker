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
    """The folder to scan: server's suggestion, local override, then discovery."""
    return directory or config.activities_dir or paths.activities_dir()


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
            if name.lower() == _IN_PROGRESS:
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

    async def activities_read(path: str = "") -> dict:
        """Return one activity file's bytes, base64-encoded.

        Confined to the Activities folder. The server chooses this path from a
        listing this connector produced, so a path outside it means either a
        bug or a server that should not be trusted with arbitrary local reads
        - either way, refuse.
        """
        target = _resolved_activities_dir(config, None)
        if not target or not _within(target, path):
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
        zwift_id: str = "me",
        override: Optional[str] = None,
        write: Optional[List[dict]] = None,
        remove: Optional[List[str]] = None,
        require_existing: bool = False,
        resolution: str = "resolve",
    ) -> dict:
        """Make the Zwift custom-workout folder match the server's manifest."""
        effective_override = override or config.workouts_dir
        if resolution == "direct":
            target = paths.workouts_dir(zwift_id, override=effective_override)
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
        if write:
            result = zwo.write_plan_to_zwift(
                write, zwift_id or "me", workouts_override=target
            )
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

    Character-for-character the check the single-machine install runs in
    ``backend/local.py``; it lives here as well because in a server/client
    install the folders being judged are *these*, and the server's home
    directory has no bearing on them.
    """
    raw = (value or "").strip()
    if not raw:
        return "", None
    expanded = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    if require_exists and not os.path.isdir(expanded):
        return None, f"Folder not found or not a directory: {raw}"
    for root in paths.trusted_storage_roots():
        resolved_root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
        try:
            if os.path.commonpath([expanded, resolved_root]) == resolved_root:
                return expanded, None
        except ValueError:
            continue  # Different Windows drives or UNC shares.
    return None, (
        "Folder must be inside your home directory or a configured "
        f"Zwift data directory: {raw}"
    )
