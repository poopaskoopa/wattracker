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


_OUTSIDE_ROOTS = (
    "Folder must be inside your home directory or a configured "
    "Zwift data directory: {raw}"
)


def _path_key(path: str) -> str:
    """Comparison key for two paths naming the same folder (case-folded on NT)."""
    if sys.platform.startswith("win"):
        return ntpath.normcase(ntpath.normpath(path))
    return os.path.normpath(path)


def export_workouts_roots() -> List[str]:
    """The roots .zwo export enumerates player folders from.

    ONE definition, used by resolve_export_dir() to look an id up and by
    confine_detected_dir() to decide whether a folder was discovered under a
    root at all. If those two ever disagreed about what "a Workouts root" is,
    the confinement exception would apply somewhere the resolver never looks,
    or fail to apply where it does.
    """
    override = os.environ.get("WATTRACKER_ZWIFT_WORKOUTS_ROOT")
    return [override] if override else candidate_workouts_roots()


def _within_trusted_roots(candidate: str) -> bool:
    """True when ``candidate`` (already absolute) sits under a trusted root."""
    for root in trusted_storage_roots():
        try:
            resolved_root = os.path.realpath(
                os.path.abspath(os.path.expanduser(root))
            )
            if os.path.commonpath([candidate, resolved_root]) == resolved_root:
                return True
        except ValueError:
            continue  # Different Windows drives / UNC shares, or a NUL byte.
    return False


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

    THE ONE PLACE this rule lives for SUBMITTED paths - every route or stored
    value that turns into a filesystem path must come through here, or the
    confinement is only as good as the least careful call site. Paths the app
    DISCOVERED by enumerating a root it already trusts go through
    confine_detected_dir() instead; see there for why they are a different
    trust class and what stays identical between the two.
    """
    raw = (value or "").strip()
    if not raw:
        return "", None
    try:
        expanded = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    except (OSError, ValueError):
        # An embedded NUL (or anything else the OS refuses to canonicalise) is
        # not a folder; it must not escape as an exception from a validator.
        return None, f"Folder not found or not a directory: {raw}"
    if must_exist and not os.path.isdir(expanded):
        return None, f"Folder not found or not a directory: {raw}"
    if _within_trusted_roots(expanded):
        return expanded, None
    return None, _OUTSIDE_ROOTS.format(raw=raw)


def confine_detected_dir(
    value: Optional[str],
) -> "tuple[Optional[str], Optional[str]]":
    """Validate a folder the app DISCOVERED inside a root it already trusts.

    Same containment rule as confine_storage_dir() with one deliberate
    difference: every ancestor is resolved, the final component is not. A
    directory ENTRY that lives in a trusted Workouts root is accepted even when
    it is a junction/symlink onto another volume.

    Why that is not a hole, and why the strict rule cannot simply be reused:

    * Relocating ``...\\Zwift\\Workouts\\<player id>`` to another drive with
      ``mklink /J`` (or a symlink on macOS/Linux) is a supported, common Zwift
      setup. Under the strict rule the rider's own folder came back "blocked",
      which is both wrong and, because resolve_export_dir() did not apply the
      same rule, a resolver/writer disagreement that reached routes as a 500.
    * The threat confine_storage_dir() exists to stop is a SUBMITTED string -
      a Settings field, a rescan body, a DB row from a restored backup or an
      older release - naming somewhere the user never chose. Nothing submitted
      selects this path: the name comes from os.listdir() of a trusted root, or
      from a safe_zwift_id() that must already exist as an entry there.
    * Creating such a link requires write access to the user's own Zwift
      Workouts folder. An attacker who has that does not need this function -
      they can replace the .zwo files, or the whole folder, directly. Following
      the link is honouring the user's filesystem layout, not widening reach.
    * Leniency stops at the leaf. Ancestors are realpath()'d before the check,
      so a path that leaves the trusted roots higher up is still refused, and a
      relocated Workouts root is accepted only because that root is itself a
      trusted root (trusted_storage_roots() resolves its own entries).
    * Leniency also stops at the Workouts roots. The DIRECT parent must be one
      of export_workouts_roots(), not merely somewhere under a trusted root, so
      the exception covers the one shape it was written for - a player folder
      immediately inside a Zwift Workouts root - and cannot be inherited by
      some future caller passing an arbitrary path under $HOME. Anything else
      falls back to the strict rule below rather than to a looser one.

    Note the last component is NOT resolved on purpose, so the value handed to
    the writer is the path the user recognises and the one Zwift itself reads.
    """
    raw = (value or "").strip()
    if not raw:
        return None, "No folder to check"
    absolute = os.path.abspath(os.path.expanduser(raw))
    parent, name = os.path.split(absolute)
    if not name or name in (".", ".."):
        # Not a leaf entry under a parent - there is nothing to be lenient
        # about, so fall back to the strict rule rather than inventing one.
        return confine_storage_dir(absolute, must_exist=True)
    try:
        real_parent = os.path.realpath(parent)
        entry = os.path.join(real_parent, name)
        exists = os.path.isdir(entry)
    except (OSError, ValueError):
        return None, f"Folder not found or not a directory: {raw}"
    if _path_key(real_parent) not in {
        _path_key(os.path.realpath(root)) for root in export_workouts_roots()
    }:
        return confine_storage_dir(absolute, must_exist=True)
    if not exists:
        # Discovered folders exist by construction; one that does not is a
        # dangling link or a race, and is not a usable export target.
        return None, f"Folder not found or not a directory: {raw}"
    if _within_trusted_roots(entry):
        return entry, None
    return None, _OUTSIDE_ROOTS.format(raw=raw)


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
        roots = export_workouts_roots()
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


# Reasons whose directory the app DISCOVERED by enumerating a trusted root,
# rather than being told to use by a stored/typed value. Only these earn
# confine_detected_dir()'s leniency about a final-component junction/symlink.
_DISCOVERED_REASONS = frozenset({"detected", "zwift_id"})


def confine_export_dir(
    directory: Optional[str], reason: str
) -> "tuple[Optional[str], Optional[str]]":
    """Apply the confinement rule the branch that produced ``directory`` earns.

    ONE function so resolve_export_dir() and workouts_dir() cannot drift apart:
    the resolver runs it on the way out and the writer runs it again on the way
    in, and because it is keyed on the same ``reason``, the second run can only
    ever agree with the first. Two path decisions with different rules living
    next to each other is exactly the bug issue #44 is about; the fix is not to
    have a second rule, it is to have one rule with an explicit trust input.
    """
    if reason in _DISCOVERED_REASONS:
        return confine_detected_dir(directory)
    return confine_storage_dir(directory, must_exist=False)


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

    This is THE resolver for both export paths: the automatic plan-export sweep
    reads (directory, reason) straight from here, and workouts_dir() wraps it
    for the write path, turning a (None, reason) into a refusal the writer
    cannot mistake for a directory.

    EVERY branch returns a directory that has already passed
    confine_export_dir(), so a truthy return here is a directory workouts_dir()
    will accept. It used to return the detected folder unchecked while
    workouts_dir() re-checked it strictly, which meant a rider whose player
    folder was relocated to another drive got a directory from the resolver and
    a refusal from the writer - and every caller that pre-resolved a target and
    passed it as ``workouts_override`` handed the writer a folder it refused.
    """
    if override:
        clean = confined_stored_dir(override, "workouts_dir")
        if not clean:
            return None, "blocked"
        return clean, "override"
    env_override = os.environ.get("WATTRACKER_WORKOUTS_DIR")
    if env_override:
        return _confined_or_blocked(env_override, "override")
    # An unusable id is treated as no id at all: fall through to detection, so
    # the caller reports 'choose'/'missing' and the UI sends the user to the
    # player-folder picker instead of joining a traversing value onto a root.
    safe_id = safe_zwift_id(zwift_id)
    if zwift_id and not safe_id:
        log.warning("ignoring unusable zwift_id for export dir: %r", zwift_id)
    if safe_id:
        for root in export_workouts_roots():
            directory = os.path.join(root, safe_id)
            if os.path.isdir(directory):
                return _confined_or_blocked(directory, "zwift_id")
    candidates = candidate_zwift_ids()
    if len(candidates) == 1:
        return _confined_or_blocked(candidates[0]["path"], "detected")
    if candidates:
        return None, "choose"
    return None, "missing"


def _confined_or_blocked(directory: str, reason: str) -> "tuple[Optional[str], str]":
    """(confined directory, reason), or (None, 'blocked') if it is refused."""
    clean, err = confine_export_dir(directory, reason)
    if not clean:
        log.warning("refusing an export dir outside the trusted roots: %s", err)
        return None, "blocked"
    return clean, reason


class ExportTargetUnavailable(RuntimeError):
    """No directory could be handed to a .zwo writer.

    ``reason`` uses the resolve_export_dir() vocabulary so the caller can
    render the same messages for both export paths:

    * ``"blocked"`` - a target WAS determined and was refused as unsafe (a
      stored/typed workouts_dir that escapes the trusted roots, directly or
      through a symlink). The user must fix the folder they configured.
    * ``"choose"`` - several Zwift player folders exist and guessing between
      them would export into the wrong rider's folder.
    * ``"missing"`` - no Zwift player folder was found at all.

    ``refused`` separates the "we found somewhere and said no" case from the
    "we found nowhere" case without the caller matching on strings.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            detail or f"no usable Zwift workouts directory ({reason})"
        )

    @property
    def refused(self) -> bool:
        """True when a target was determined but rejected as unsafe."""
        return self.reason == "blocked"


def workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    """Confined folder to write this user's .zwo files into, or refuse.

    Returns a directory that has passed confine_export_dir(), or raises
    ExportTargetUnavailable(reason). There is no fallback target: writing to a
    guessed folder is worse than not writing at all, because Zwift only reads
    the real player folder, so the export "succeeds", the UI reports a path,
    and the workouts never show up in the game. A refusal the caller has to
    handle is the one outcome that cannot be mistaken for a successful export.

    This used to join ``safe_zwift_id(zwift_id) or "me"`` onto the first
    Workouts root, which is where that failure came from: with no zwift_id set,
    every explicit export button wrote into ...\\Zwift\\Workouts\\me\\ and
    reported success. The literal "me" is gone; so is the old behaviour of
    quietly falling back to that default when the stored workouts_dir escaped
    the trusted roots or the stored zwift_id was not a bare folder name.

    Confinement is not weakened by dropping the fallback, it is tightened:

    * The target is resolved by resolve_export_dir(), so the explicit export
      buttons and the automatic plan-export sweep now agree on where a user's
      .zwo files go and on why they sometimes cannot go anywhere.
    * An escaping ``override`` is refused there ("blocked") instead of being
      swapped for a default, so a configured folder is never silently replaced.
    * An unusable ``zwift_id`` is never joined onto a root; it is logged and
      treated as no id at all, which leaves detection to produce a real player
      folder or this function to refuse. It cannot produce a path.
    * Whatever comes back is re-checked with confine_export_dir() before any
      caller can makedirs()/open() it. That is deliberately redundant with
      resolve_export_dir(): it means the write path cannot end up in a state
      where no confinement check ran, whatever a future resolver branch does.
      It re-checks with the SAME rule the resolver's branch used - keyed on the
      returned ``reason`` - so redundant here means redundant, not stricter. A
      re-check that could reject what the resolver just returned is not a
      safety net, it is a second, disagreeing path decision, and it is what
      turned a relocated Zwift folder into unhandled 500s on two routes.

    ``..``, absolute and UNC paths are handled by that check, which compares
    realpaths against trusted_storage_roots(). Symlinks are too, with one
    documented exception: a junction/symlink as the final component of a folder
    the app DISCOVERED inside a trusted Workouts root is followed, because that
    is how a relocated Zwift folder looks. See confine_detected_dir().
    """
    directory, reason = resolve_export_dir(zwift_id=zwift_id, override=override)
    if not directory:
        raise ExportTargetUnavailable(reason)
    # Belt and braces: the resolver's own branches already ran this check, so
    # this only fires if one of them ever stops doing so.
    clean, err = confine_export_dir(directory, reason)
    if not clean:
        log.warning("refusing an export dir outside the trusted roots: %s", err)
        raise ExportTargetUnavailable("blocked", err or "")
    return clean


def ensure_workouts_dir(zwift_id: Optional[str] = None, override: Optional[str] = None) -> str:
    """workouts_dir() plus makedirs(). Raises ExportTargetUnavailable, and in
    that case creates nothing - the refusal happens before any directory is
    created."""
    directory = workouts_dir(zwift_id=zwift_id, override=override)
    os.makedirs(directory, exist_ok=True)
    return directory
