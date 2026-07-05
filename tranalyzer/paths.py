"""Cross-platform Zwift folder discovery.

macOS:   ~/Documents/Zwift/Activities  and  ~/Documents/Zwift/Workouts/<ZwiftID>
Windows: %LOCALAPPDATA%/Zwift/Activities  (+ legacy ~/Documents/Zwift/Activities)
Linux:   best-effort ~/Documents/Zwift/...

Per-user settings overrides always win.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional


def _home() -> str:
    return os.path.expanduser("~")


def _documents_zwift() -> str:
    return os.path.join(_home(), "Documents", "Zwift")


def candidate_activities_dirs() -> List[str]:
    """Per-OS candidate Activities directories, in priority order."""
    candidates: List[str] = []
    if sys.platform == "darwin":
        candidates.append(os.path.join(_documents_zwift(), "Activities"))
    elif sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Zwift", "Activities"))
        candidates.append(os.path.join(_documents_zwift(), "Activities"))  # legacy
    else:  # linux / other - best effort
        candidates.append(os.path.join(_documents_zwift(), "Activities"))
    return candidates


def annotated_candidates() -> List[dict]:
    """Candidate Activities directories annotated with an ``exists`` flag."""
    return [
        {"path": c, "exists": os.path.isdir(c)} for c in candidate_activities_dirs()
    ]


def activities_dir(override: Optional[str] = None) -> str:
    """Resolve the Activities directory. A per-user override wins.

    Returns the first existing candidate, else the first candidate.
    """
    if override:
        return override
    for c in candidate_activities_dirs():
        if os.path.isdir(c):
            return c
    return candidate_activities_dirs()[0]


def zwift_workouts_root() -> str:
    """The Zwift Workouts root that contains per-player-ID subfolders.

    Overridable via TRANALYZER_ZWIFT_WORKOUTS_ROOT (tests point it at a temp
    dir so player-folder detection never touches the real Zwift install).
    """
    override = os.environ.get("TRANALYZER_ZWIFT_WORKOUTS_ROOT")
    if override:
        return override
    return os.path.join(_documents_zwift(), "Workouts")


def candidate_zwift_ids(root: Optional[str] = None) -> List[dict]:
    """Detected Zwift player-ID folders under Documents/Zwift/Workouts.

    Zwift only reads custom workouts from Workouts/<numeric player id>/, so
    candidates are the positive-numeric subfolders, most recently used first
    (folder mtime). Each entry: {"zwift_id", "path", "mtime"}.
    """
    root = root or zwift_workouts_root()
    out: List[dict] = []
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        if not name.isdigit():  # excludes 'Downloaded', junk like '-42'
            continue
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0.0
        out.append({"zwift_id": name, "path": p, "mtime": mtime})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def resolve_export_dir(
    zwift_id: Optional[str] = None, override: Optional[str] = None
) -> "tuple[Optional[str], str]":
    """Resolve where plan .zwo files should be exported, without guessing.

    Returns (directory, reason). Reasons:
      - 'override'  : the user's workouts_dir setting
      - 'zwift_id'  : Workouts/<their zwift_id> exists on disk
      - 'detected'  : exactly one player-ID folder exists, use it
      - 'choose'    : several candidates - the user must pick in Settings
      - 'missing'   : no Zwift Workouts folders found at all
    Directory is None for 'choose'/'missing'.
    """
    if override:
        return override, "override"
    if zwift_id:
        d = os.path.join(zwift_workouts_root(), str(zwift_id))
        if os.path.isdir(d):
            return d, "zwift_id"
    candidates = candidate_zwift_ids()
    if len(candidates) == 1:
        return candidates[0]["path"], "detected"
    if candidates:
        return None, "choose"
    return None, "missing"


def workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    """Resolve the Zwift Workouts directory for a given ZwiftID.

    A per-user override wins; otherwise per-OS default under Documents/Zwift.
    """
    if override:
        return override
    zid = zwift_id or "me"
    return os.path.join(_documents_zwift(), "Workouts", str(zid))


def ensure_workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    """Return the workouts dir, creating it if missing."""
    d = workouts_dir(zwift_id=zwift_id, override=override)
    os.makedirs(d, exist_ok=True)
    return d
