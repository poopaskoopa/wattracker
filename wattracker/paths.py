"""Cross-platform Zwift folder discovery."""
from __future__ import annotations

import ctypes
import logging
import ntpath
import os
import sys
import uuid
from typing import List, Optional

log = logging.getLogger(__name__)


def _home() -> str:
    return os.path.expanduser("~")


def _windows_documents_known_folder() -> Optional[str]:
    """Return Windows' redirected Documents known folder, if available."""
    if not sys.platform.startswith("win"):
        return None
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        raw = uuid.UUID("fdd39ad0-238f-46af-adb4-6c85480369c7").bytes_le
        folder_id = GUID.from_buffer_copy(raw)
        value = ctypes.c_void_p()
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(value)
        )
        if result != 0 or not value.value:
            return None
        try:
            return ctypes.wstring_at(value.value)
        finally:
            ole32.CoTaskMemFree(value)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _dedupe(paths: List[str], windows: bool = False) -> List[str]:
    out: List[str] = []
    seen = set()
    for path in paths:
        if not path:
            continue
        normalized = os.path.normpath(path)
        key = ntpath.normcase(ntpath.normpath(normalized)) if windows else normalized
        if key not in seen:
            seen.add(key)
            out.append(normalized)
    return out


def candidate_documents_dirs() -> List[str]:
    """Documents roots, including Windows redirection and OneDrive variants."""
    if not sys.platform.startswith("win"):
        return [os.path.join(_home(), "Documents")]
    candidates: List[str] = []
    known = _windows_documents_known_folder()
    if known:
        candidates.append(known)
    for name in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        root = os.environ.get(name)
        if root:
            candidates.append(os.path.join(root, "Documents"))
    profile = os.environ.get("USERPROFILE")
    if profile:
        candidates.append(os.path.join(profile, "Documents"))
    candidates.append(os.path.join(_home(), "Documents"))
    return _dedupe(candidates, windows=True)


def candidate_activities_dirs() -> List[str]:
    """Per-OS candidate Activities directories, in priority order."""
    candidates: List[str] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Zwift", "Activities"))
    candidates.extend(
        os.path.join(root, "Zwift", "Activities")
        for root in candidate_documents_dirs()
    )
    return _dedupe(candidates, windows=sys.platform.startswith("win"))


def annotated_candidates() -> List[dict]:
    return [{"path": c, "exists": os.path.isdir(c)} for c in candidate_activities_dirs()]


def _first_existing(candidates: List[str]) -> str:
    if not candidates:
        raise RuntimeError("no path candidates available")
    return next((path for path in candidates if os.path.isdir(path)), candidates[0])


def activities_dir(override: Optional[str] = None) -> str:
    """Resolve Activities: explicit override, first existing, then first candidate."""
    if override:
        return override
    env_override = os.environ.get("WATTRACKER_ACTIVITIES_DIR")
    if env_override:
        return env_override
    return _first_existing(candidate_activities_dirs())


def candidate_workouts_roots() -> List[str]:
    return _dedupe(
        [os.path.join(root, "Zwift", "Workouts") for root in candidate_documents_dirs()],
        windows=sys.platform.startswith("win"),
    )


def trusted_storage_roots() -> List[str]:
    """Roots the Settings UI may accept for activity/workout directories.

    The home directory remains trusted, while Windows Known Folder/OneDrive
    redirects and process-owner environment overrides may legitimately live on
    another drive or UNC share. Callers must still resolve symlinks and require
    the submitted directory to exist before checking containment.
    """
    candidates = [_home()]
    candidates.extend(candidate_documents_dirs())
    candidates.extend(candidate_activities_dirs())
    candidates.extend(candidate_workouts_roots())
    for name in (
        "WATTRACKER_ACTIVITIES_DIR",
        "WATTRACKER_WORKOUTS_DIR",
        "WATTRACKER_ZWIFT_WORKOUTS_ROOT",
    ):
        value = os.environ.get(name)
        if value:
            candidates.append(value)
    return _dedupe(candidates, windows=sys.platform.startswith("win"))


def confine_storage_dir(
    value: Optional[str], must_exist: bool = True
) -> "tuple[Optional[str], Optional[str]]":
    """Validate a user-supplied folder path. Returns (clean_path, error).

    A folder is accepted only when its real path stays under the user's home,
    an OS-discovered Documents/Zwift root, or a process-owner environment
    override (trusted_storage_roots()). This admits redirected Windows Known
    Folders and OneDrive/UNC roots without permitting arbitrary web-supplied
    system paths or symlink escapes. Empty means "unchanged" ("", None).

    ``must_exist`` additionally requires the folder to be there already. The
    Settings form requires it (a typo should not be saved); the rescan endpoint
    does not, because "the folder you named is not on this machine" is a state
    its status panel is built to report - and scanning a path that does not
    exist reads nothing either way. Containment is checked in both cases: the
    realpath of a not-yet-existing path still resolves every symlink that does
    exist along it and still normalises ``..`` away.

    THE ONE PLACE this rule lives - every route or stored value that turns into
    a filesystem path must come through here, or the confinement is only as
    good as the least careful call site.
    """
    raw = (value or "").strip()
    if not raw:
        return "", None
    expanded = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    if must_exist and not os.path.isdir(expanded):
        return None, f"Folder not found or not a directory: {raw}"
    for root in trusted_storage_roots():
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


def confined_stored_dir(value: Optional[str], what: str = "directory") -> Optional[str]:
    """Return a directory setting read from the DB, or None if it escapes.

    Validating on write is not enough on its own. A row can predate the write-
    side check (POST /activities/rescan persisted whatever was posted until
    this commit), or arrive from a restored backup, or from a hand-edited DB -
    so a value is not trustworthy merely because it is already stored. This is
    the read-side half: whatever is in the row, the path actually used is
    confined, and a rejected one is logged loudly rather than silently obeyed.
    """
    clean, err = confine_storage_dir(value, must_exist=False)
    if err:
        log.warning("ignoring stored %s outside the trusted roots: %s", what, err)
        return None
    return clean or None


# A Zwift player id is used as ONE folder name under a trusted Workouts root,
# never as a path. Anything that could make os.path.join() leave that root -
# a separator, a parent reference, an absolute path, a Windows drive - is not
# an id. Note the checks are platform-independent on purpose: a value stored on
# one OS must not become traversal after the DB is restored on another.
_MAX_ZWIFT_ID_LEN = 64


def safe_zwift_id(value: Optional[str]) -> Optional[str]:
    """Return the zwift id if it is usable as a single folder name, else None."""
    # Callers historically passed whatever the DB / Zwift API handed back, so
    # coerce rather than assume str (the old code did str(zwift_id or "me")).
    raw = str(value).strip() if value is not None else ""
    if not raw or len(raw) > _MAX_ZWIFT_ID_LEN:
        return None
    if raw in (".", ".."):
        return None
    if any(sep in raw for sep in ("/", "\\", ":", "\0")):
        return None
    # Belt and braces: whatever the platform thinks, it must be a bare name.
    if os.path.isabs(raw) or ntpath.isabs(raw):
        return None
    if os.path.basename(raw) != raw or ntpath.basename(raw) != raw:
        return None
    if any(ord(ch) < 32 for ch in raw):
        return None
    return raw


def zwift_workouts_root() -> str:
    override = os.environ.get("WATTRACKER_ZWIFT_WORKOUTS_ROOT")
    if override:
        return override
    return _first_existing(candidate_workouts_roots())


def candidate_zwift_ids(root: Optional[str] = None) -> List[dict]:
    if root:
        roots = [root]
    else:
        exact_override = os.environ.get("WATTRACKER_WORKOUTS_DIR")
        if exact_override:
            name = os.path.basename(os.path.normpath(exact_override))
            if name.isdigit() and os.path.isdir(exact_override):
                try:
                    mtime = os.path.getmtime(exact_override)
                except OSError:
                    mtime = 0.0
                return [{
                    "zwift_id": name,
                    "path": exact_override,
                    "mtime": mtime,
                }]
            return []
        override = os.environ.get("WATTRACKER_ZWIFT_WORKOUTS_ROOT")
        roots = [override] if override else candidate_workouts_roots()
    out: List[dict] = []
    seen = set()
    for candidate_root in roots:
        if not candidate_root or not os.path.isdir(candidate_root):
            continue
        for name in os.listdir(candidate_root):
            path = os.path.join(candidate_root, name)
            key = ntpath.normcase(ntpath.normpath(path)) if sys.platform.startswith("win") else os.path.normpath(path)
            if key in seen or not os.path.isdir(path) or not name.isdigit():
                continue
            seen.add(key)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            out.append({"zwift_id": name, "path": path, "mtime": mtime})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def resolve_export_dir(
    zwift_id: Optional[str] = None, override: Optional[str] = None
) -> "tuple[Optional[str], str]":
    """Resolve where this user's .zwo files go. Returns (directory, reason).

    ``override`` is the stored ``user_settings.workouts_dir``. It is confined
    here rather than trusted: the Settings form validates on write, but a row
    can predate that check, come from a restored backup, or be hand-edited, and
    the value ends up in os.makedirs() + open(..., "w"). A rejected override
    returns (None, "blocked") instead of quietly falling through to detection -
    a user who configured a folder must be told it was refused, not silently
    have their workouts written somewhere else.
    """
    if override:
        clean = confined_stored_dir(override, "workouts_dir")
        if not clean:
            return None, "blocked"
        return clean, "override"
    env_override = os.environ.get("WATTRACKER_WORKOUTS_DIR")
    if env_override:
        return env_override, "override"
    # An unusable id is treated as no id at all: fall through to detection, so
    # the caller reports 'choose'/'missing' and the UI sends the user to the
    # player-folder picker instead of joining a traversing value onto a root.
    safe_id = safe_zwift_id(zwift_id)
    if zwift_id and not safe_id:
        log.warning("ignoring unusable zwift_id for export dir: %r", zwift_id)
    if safe_id:
        roots = (
            [os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"]]
            if os.environ.get("WATTRACKER_ZWIFT_WORKOUTS_ROOT")
            else candidate_workouts_roots()
        )
        for root in roots:
            directory = os.path.join(root, safe_id)
            if os.path.isdir(directory):
                return directory, "zwift_id"
    candidates = candidate_zwift_ids()
    if len(candidates) == 1:
        return candidates[0]["path"], "detected"
    if candidates:
        return None, "choose"
    return None, "missing"


def workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    """Folder to write this user's .zwo files into.

    The zwift_id is joined onto the Workouts root, and callers (zwo.write_*)
    makedirs() the result, so an id that is not a bare folder name is an
    arbitrary-directory-create plus arbitrary .zwo write. Reject it here, at the
    join itself, and fall back to the same default an empty id gets: the
    write-side check in db.save_user_settings does not help a row that predates
    it or came from a restored backup.

    The same reasoning applies to ``override`` (the stored workouts_dir), which
    is the stronger primitive of the two - it is the whole path, not one
    component. It is confined here for the same reason and, like an unusable
    id, an escaping override falls back to the default rather than raising:
    callers report the directory they actually wrote to, so the fallback is
    visible to the user instead of silent.
    """
    if override:
        clean = confined_stored_dir(override, "workouts_dir")
        if clean:
            return clean
    env_override = os.environ.get("WATTRACKER_WORKOUTS_DIR")
    if env_override:
        return env_override
    safe_id = safe_zwift_id(zwift_id)
    if zwift_id and not safe_id:
        log.warning("ignoring unusable zwift_id for workouts dir: %r", zwift_id)
    return os.path.join(zwift_workouts_root(), safe_id or "me")


def ensure_workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    directory = workouts_dir(zwift_id=zwift_id, override=override)
    os.makedirs(directory, exist_ok=True)
    return directory
