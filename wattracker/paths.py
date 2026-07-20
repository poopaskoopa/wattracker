"""Cross-platform Zwift folder discovery."""
from __future__ import annotations

import ctypes
import ntpath
import os
import sys
import uuid
from typing import List, Optional


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
    if override:
        return override, "override"
    env_override = os.environ.get("WATTRACKER_WORKOUTS_DIR")
    if env_override:
        return env_override, "override"
    if zwift_id:
        roots = (
            [os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"]]
            if os.environ.get("WATTRACKER_ZWIFT_WORKOUTS_ROOT")
            else candidate_workouts_roots()
        )
        for root in roots:
            directory = os.path.join(root, str(zwift_id))
            if os.path.isdir(directory):
                return directory, "zwift_id"
    candidates = candidate_zwift_ids()
    if len(candidates) == 1:
        return candidates[0]["path"], "detected"
    if candidates:
        return None, "choose"
    return None, "missing"


def workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    if override:
        return override
    env_override = os.environ.get("WATTRACKER_WORKOUTS_DIR")
    if env_override:
        return env_override
    return os.path.join(zwift_workouts_root(), str(zwift_id or "me"))


def ensure_workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    directory = workouts_dir(zwift_id=zwift_id, override=override)
    os.makedirs(directory, exist_ok=True)
    return directory
