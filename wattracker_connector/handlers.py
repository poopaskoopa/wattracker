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

# The only extension workouts.sync ever writes, and therefore the only one it
# will delete.
_EXPORT_SUFFIX = ".zwo"


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


def activities_scope(config: ConnectorConfig) -> List[str]:
    """Every Activities folder this connector will act on, best guess first.

    Derived entirely from THIS machine - the connector's own config, its
    environment, and Zwift's own per-OS locations - so nothing the server sends
    can extend it. A server-supplied folder is not a folder to use, it is a
    choice BETWEEN these; see ``_resolved_activities_dir``.
    """
    dirs = [config.activities_dir] if config.activities_dir else []
    dirs.append(paths.activities_dir())
    dirs.extend(paths.candidate_activities_dirs())
    return [d for d in dirs if d]


def _resolved_activities_dir(config: ConnectorConfig,
                             directory: Optional[str]) -> Optional[str]:
    """The folder to work in: one of this machine's own, and nothing else.

    A folder the server names is honoured only when it *is* one of the folders
    in ``activities_scope`` - which is how a rider with two Zwift installs
    picks between them in the web UI - and is otherwise refused. Returns
    ``None`` in that case, which every caller treats as "no such folder".

    It used to be enough for a server-supplied folder to clear
    ``confine_storage_dir``, i.e. to sit anywhere under this machine's trusted
    roots. That is the right rule for a path the rider typed on the machine it
    describes, and the wrong one for a path that arrived over the wire: it made
    the whole home directory one trust domain, so naming any folder holding a
    ``.fit`` read it back, and ``activities.list`` answered as a
    directory-existence and symlink-target oracle for the rest. A path that
    arrived over RPC is confined to the folder it is FOR, not to $HOME.

    The consequence is deliberate: an Activities folder somewhere unusual is
    configured on the connector (``--activities-dir``), by the person sitting
    at the machine, and cannot be set from the server side. That is the trust
    model rather than an oversight - the server cannot name a folder because
    naming folders is exactly the capability being withheld from it. The web
    UI refuses such a value when it is saved (``paths.validate_dir`` runs this
    same scope check), so it fails where the rider can see it rather than
    silently scanning nothing.

    Listing and reading must resolve through *this* function identically. They
    did not always: ``activities.read`` used to ignore ``directory`` and check
    against the local folder alone, so whenever the server carried an
    ``activities_dir`` override that differed from the connector's own, the
    listing succeeded and then every read of the files it had just offered was
    refused.
    """
    scope = activities_scope(config)
    if not directory:
        return scope[0] if scope else None
    for candidate in scope:
        if _same_folder(candidate, directory):
            return candidate  # this machine's own folder, handed straight back
    log.warning(
        "refusing server-supplied activities folder %s: this connector only "
        "serves %s", directory, ", ".join(scope) or "(nothing)"
    )
    return None


def _same_folder(directory: str, candidate: str) -> bool:
    """Whether two paths name the same folder, symlinks resolved."""
    return _within(directory, candidate) and _within(candidate, directory)


def _resolved_workouts_override(
    config: ConnectorConfig, override: Optional[str]
) -> "tuple[Optional[str], Optional[str]]":
    """Which workouts folder override to honour. Returns (override, refusal).

    This machine's own config is the default and is trusted as it stands: it
    describes this machine and the person who set it was sitting at it. A
    server-supplied one is honoured only when it lands in a Zwift Workouts
    folder (``paths.within_workouts_roots``) or names the configured folder
    itself, because a Zwift Workouts folder is what ``workouts.sync`` is for.

    The server's value used to be taken whenever it cleared
    ``confine_storage_dir``, which is all of $HOME - so ``remove`` deleted any
    file the peer named under the rider's home directory (``.ssh/
    authorized_keys``, ``Documents/taxes/2025.pdf``) and ``write`` created
    folders anywhere in it. Hardening the filename, as the previous round did,
    is worth nothing while the directory it joins onto is the peer's to choose.

    A refusal is returned as a refusal, never as "no override": falling through
    to detection would export somewhere the rider did not configure and report
    success, which is issue #44's failure mode arriving by a new route.
    """
    if not override:
        return config.workouts_dir, None
    if config.workouts_dir and _same_folder(config.workouts_dir, override):
        return config.workouts_dir, None
    clean, error = validate_dir(override, require_exists=False)
    # Scope is judged on the value as submitted, not on the confined one:
    # confine_storage_dir resolves the whole path including the last component,
    # and a player folder junctioned onto another drive - a supported Zwift
    # layout - resolves out of the Workouts root it plainly sits in.
    if clean and not paths.within_workouts_roots(override):
        clean, error = None, (
            f"not a Zwift Workouts folder on this machine: {override}"
        )
    if not clean:
        log.warning("refusing server-supplied workouts folder: %s", error)
        return None, error or f"refused: {override}"
    return clean, None


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


def is_activity_file(name: str) -> bool:
    """Whether a filename is one this connector will ever hand over.

    A ``.fit``, and never Zwift's live recording buffer. It used to live only
    inside ``activities.list``'s glob, which meant the read path enforced no
    such thing: it checked containment and nothing else, so any file at all
    under a folder the server named came back - an ssh key, or this
    connector's own config file with the device token in it.

    Public because ``watcher`` applies it too, and must apply exactly this one:
    a watcher that reported a file the listing would refuse to offer would ask
    the server to scan for something it can never import.
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
        and is_activity_file(os.path.basename(resolved))
    )


def build_handlers(config: ConnectorConfig) -> Dict[str, Callable]:
    """The method table this connector answers to."""

    # ------------------------------------------------------- discovery
    async def paths_activity_candidates() -> List[dict]:
        return paths.annotated_candidates()

    async def paths_zwift_id_candidates() -> List[dict]:
        return paths.candidate_zwift_ids()

    async def paths_default_activities_dir() -> Optional[str]:
        # The head of the same scope the listing resolves through, so what the
        # settings page offers as the default is a folder this connector will
        # actually serve.
        return _resolved_activities_dir(config, None)

    async def paths_workouts_root() -> Optional[str]:
        return paths.zwift_workouts_root()

    async def paths_resolve_export_dir(
        zwift_id: Optional[str] = None, override: Optional[str] = None
    ) -> dict:
        # The same override rule workouts.sync applies, so the folder the
        # settings page reports is the folder an export would actually use. A
        # resolver and a writer that disagree is issue #44's shape.
        effective, refusal = _resolved_workouts_override(config, override)
        if refusal:
            return {"directory": None, "reason": "blocked"}
        directory, reason = paths.resolve_export_dir(zwift_id, effective)
        return {"directory": directory, "reason": reason}

    async def paths_validate_dir(
        value: str = "", require_exists: bool = True, scope: str = ""
    ) -> dict:
        """Judge a folder the rider typed into the web UI, on its own machine.

        ``scope`` names the field it was typed into, so the answer is the same
        one the handler for that field will give later. Without it a rider
        could save an activities folder that cleared the trusted roots and then
        watch every scan of it come back empty, because the listing applies the
        narrower scope rule and the settings form did not - a value that is
        accepted and then ignored is worse than one that is refused.
        """
        clean, error = validate_dir(value, require_exists=require_exists)
        if clean:
            clean, error = _in_field_scope(config, scope, value, clean)
        return {"clean": clean, "error": error}

    # -------------------------------------------------- activity files
    async def activities_list(directory: Optional[str] = None) -> dict:
        target = _resolved_activities_dir(config, directory)
        if not target or not os.path.isdir(target):
            # Echo back the folder that was asked for when there is no resolved
            # one, so the scan status names the folder it could not use rather
            # than reporting an empty string.
            return {"directory": target or directory, "exists": False,
                    "files": [], "skipped": 0}

        found: List[str] = []
        for pattern in ("*.fit", "*.FIT"):
            found.extend(glob.glob(os.path.join(target, pattern)))

        files: List[dict] = []
        skipped = 0
        for path in sorted(set(found)):
            name = os.path.basename(path)
            if name.lower() == _IN_PROGRESS:
                skipped += 1  # expected every time Zwift is recording
                continue
            if not _in_scope(target, path):
                # Almost always a symlink pointing out of the folder. Named in
                # the log because the count alone travels to the server as a
                # bare number, where it reads as "skipped" among duplicates -
                # a rider with a symlinked Activities folder needs to be able
                # to find out WHICH file and why, and this is the only machine
                # that knows.
                log.warning(
                    "not offering %s: it does not resolve to an activity file "
                    "in %s", name, target
                )
                skipped += 1
                continue
            try:
                st = os.stat(path)
            except OSError:
                log.warning("not offering %s: it could not be read", name)
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
        directly in one of THIS machine's Activities folders, and nothing else.
        The server chooses this path from a listing this connector produced, so
        anything else means either a bug or a server that should not be trusted
        with arbitrary local reads - either way, refuse. ``directory`` is
        resolved by exactly the rule the listing used and the file is judged by
        exactly the predicate the listing used (``_in_scope``), so the two
        cannot disagree about what is in scope.

        The weaker check this replaces was containment alone, which made this a
        general file-read primitive over everything the server could name under
        this machine's trusted roots - ``~/.ssh/id_ed25519`` and this
        connector's own token file included.

        Everything after the scope check works on the RESOLVED path, so the
        file that is opened is the file that was judged. Checking one path and
        opening another is a race even when both start out the same, and the
        docstring above claims symlinks are resolved before the test - it must
        also be true of what follows it.
        """
        target = _resolved_activities_dir(config, directory)
        if not target or not _in_scope(target, path):
            raise ValueError("path is outside the activities folder")
        try:
            resolved = os.path.realpath(os.path.abspath(path))
        except (OSError, ValueError):
            raise FileNotFoundError(f"no such activity file: {path}")
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"no such activity file: {path}")
        size = os.path.getsize(resolved)
        if size > MAX_ACTIVITY_BYTES:
            raise ValueError(
                f"{os.path.basename(path)} is {size} bytes, over the "
                f"{MAX_ACTIVITY_BYTES} limit"
            )
        with open(resolved, "rb") as handle:
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
        """Make the Zwift custom-workout folder match the server's manifest.

        Both halves are a filesystem primitive pointed at a folder, so the
        folder is judged before either runs: a server-supplied override has to
        BE a Zwift Workouts folder, not merely somewhere under this machine's
        trusted roots. See ``_resolved_workouts_override``.
        """
        effective_override, refusal = _resolved_workouts_override(config, override)
        if refusal:
            return {"status": "blocked", "directory": None, "exported": 0,
                    "removed": 0, "reason": "blocked", "paths": []}
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
            # delete outside the Zwift folder, and one without a .zwo extension
            # is not something this sync ever wrote. Both halves matter - the
            # folder is a Zwift Workouts folder now, which is the rider's own
            # ride data, and "bare filename" alone would still let a peer empty
            # it of everything except .zwo files.
            if not isinstance(filename, str):
                # os.path.basename would raise on this and take the whole sync
                # with it; one bad entry is an entry to skip, not an outage.
                log.warning("refusing to remove a non-string entry %r", filename)
                continue
            if os.path.basename(filename) != filename:
                log.warning("refusing to remove suspicious filename %r", filename)
                continue
            if not filename.lower().endswith(_EXPORT_SUFFIX):
                log.warning("refusing to remove a non-export file %r", filename)
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


def _in_field_scope(
    config: ConnectorConfig, scope: str, raw: str, clean: str
) -> "tuple[Optional[str], Optional[str]]":
    """Apply the scope rule for the settings field a folder was typed into.

    The same rule the handler for that field enforces, asked at the moment the
    rider can still do something about it. The error names what to do, because
    "outside the trusted roots" is not the reason and would send them looking
    in the wrong place: on a split install these folders are configured on the
    machine running the connector.
    """
    if scope == "activities":
        if any(_same_folder(d, clean) for d in activities_scope(config)):
            return clean, None
        return None, (
            "This connector serves its own Zwift Activities folder. Choose one "
            "of the folders it found, or set a different one on the machine "
            f"running the connector (--activities-dir): {clean}"
        )
    if scope == "workouts":
        # ``raw`` for the same reason _resolved_workouts_override uses it: the
        # confined value has had its last component resolved, and a junctioned
        # player folder is a Zwift Workouts folder before that happens.
        if paths.within_workouts_roots(raw) or (
            config.workouts_dir and _same_folder(config.workouts_dir, clean)
        ):
            return clean, None
        return None, (
            "Folder must be inside the Zwift Workouts folder on the machine "
            f"running the connector: {clean}"
        )
    return clean, None


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
