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
