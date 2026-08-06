"""The default backend: Zwift lives on this machine.

Every method here is the code the app has always run inline, moved behind the
``Backend`` interface unchanged. It stays OS-agnostic by delegating to
``paths``, which already branches on the platform - nothing in this module may
assume Windows.
"""
from __future__ import annotations

import glob
import logging
import os
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

from .. import paths
from ..prescribe import zwo
from .base import ActivityFile, ActivityListing, Backend, ExportManifest

log = logging.getLogger(__name__)

# Zwift keeps this as a live recording buffer while a ride is in progress; it
# is never a finished ride, and its start second collides with the eventual
# final .fit. Filtered here so it is never cached in scanned_files either.
_IN_PROGRESS = "inprogressactivity.fit"


class LocalBackend(Backend):
    """Reads and writes the Zwift folders on the machine the app runs on."""

    name = "local"

    # ----------------------------------------------------- discovery

    def activity_candidates(self) -> List[dict]:
        return paths.annotated_candidates()

    def zwift_id_candidates(self) -> List[dict]:
        return paths.candidate_zwift_ids()

    def default_activities_dir(self) -> Optional[str]:
        return paths.activities_dir()

    def workouts_root(self) -> Optional[str]:
        return paths.zwift_workouts_root()

    def resolve_export_dir(
        self, zwift_id: Optional[str] = None, override: Optional[str] = None
    ) -> Tuple[Optional[str], str]:
        return paths.resolve_export_dir(zwift_id, override)

    def validate_dir(self, value: str) -> Tuple[Optional[str], Optional[str]]:
        """Validate a user-supplied folder path.

        Returns (clean_path, error). A folder is accepted only when it exists,
        is a directory, and its real path remains under the user's home,
        OS-discovered Documents/Zwift roots, or a process-owner environment
        override. This admits redirected Windows Known Folders and OneDrive/UNC
        roots without permitting arbitrary web-supplied system paths or symlink
        escapes. Empty means "unchanged" (clean_path="", error=None).
        """
        raw = (value or "").strip()
        if not raw:
            return "", None
        expanded = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
        if not os.path.isdir(expanded):
            return None, f"Folder not found or not a directory: {raw}"
        allowed = False
        for root in paths.trusted_storage_roots():
            resolved_root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
            try:
                if os.path.commonpath([expanded, resolved_root]) == resolved_root:
                    allowed = True
                    break
            except ValueError:
                continue  # Different Windows drives or UNC shares.
        if not allowed:
            return None, (
                "Folder must be inside your home directory or a configured "
                f"Zwift data directory: {raw}"
            )
        return expanded, None

    # ----------------------------------------------- activity files

    def list_activities(self, directory: Optional[str] = None) -> ActivityListing:
        if not directory or not os.path.isdir(directory):
            return ActivityListing(directory=directory, exists=False, files=[])

        found: List[str] = []
        for pat in ("*.fit", "*.FIT"):
            found.extend(glob.glob(os.path.join(directory, pat)))

        files: List[ActivityFile] = []
        skipped = 0
        for path in sorted(set(found)):
            name = os.path.basename(path)
            if name.lower() == _IN_PROGRESS:
                skipped += 1
                continue
            try:
                st = os.stat(path)
            except OSError:
                # Vanished or turned unreadable between glob and stat.
                skipped += 1
                continue
            files.append(
                ActivityFile(path=path, name=name, mtime=st.st_mtime, size=st.st_size)
            )
        return ActivityListing(
            directory=directory, exists=True, files=files, skipped=skipped
        )

    @contextmanager
    def readable_activity(self, path: str) -> Iterator[str]:
        """The file is already local, so hand back its own path - no copy."""
        yield path

    # ------------------------------------------------ workout files

    def apply_exports(self, manifest: ExportManifest) -> dict:
        if manifest.resolution == "direct":
            # Always yields a path, and write_plan_to_zwift creates it.
            target, reason = (
                paths.workouts_dir(manifest.zwift_id, override=manifest.override),
                "override" if manifest.override else "direct",
            )
        else:
            target, reason = self.resolve_export_dir(
                manifest.zwift_id, manifest.override
            )
        if not target:
            return {"status": reason, "directory": None, "exported": 0,
                    "removed": 0, "reason": reason, "paths": []}
        if manifest.require_existing and not os.path.isdir(target):
            # Best-effort tidy-up paths (adapt/reflow re-export) must not
            # conjure a Zwift folder that isn't there.
            return {"status": "missing", "directory": None, "exported": 0,
                    "removed": 0, "reason": "missing", "paths": []}

        exported = 0
        written: List[str] = []
        if manifest.write:
            result = zwo.write_plan_to_zwift(
                manifest.write, manifest.zwift_id or "me", workouts_override=target
            )
            # Counted from the paths rather than read from result["count"] -
            # they are the same number by construction, and one source of truth
            # beats two.
            written = result["paths"]
            exported = len(written)

        removed = 0
        for fname in manifest.remove:
            p = os.path.join(target, fname)
            try:
                if os.path.exists(p):
                    os.unlink(p)
                    removed += 1
            except OSError as e:
                log.warning("could not remove export %s: %s", p, e)

        return {"status": "ok", "directory": target, "exported": exported,
                "removed": removed, "reason": reason, "paths": written}
