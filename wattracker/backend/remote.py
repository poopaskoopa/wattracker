"""The backend that reaches the user's machine through a connector.

Every method here is a round trip to the connector over its WebSocket. The
shapes returned are identical to ``LocalBackend``'s, because the whole point
of the ``Backend`` interface is that nothing upstream should be able to tell
which one it is talking to - and ``tests/test_backend_parity.py`` holds that
line.

Failure has one name: ``ConnectorUnavailable`` (re-exported here as
``BackendUnavailable``). Callers degrade on it - the settings page says the
machine is offline, the daily sweep skips a stage - rather than 500ing. The
in-memory download routes (``/plan/{id}/download.zip`` and friends) never
touch a backend at all, so exporting by hand keeps working when the connector
is down.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import tempfile
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

from .. import connectorhub
from ..rpc import ConnectorUnavailable, RpcError
from .base import ActivityFile, ActivityListing, Backend, ExportManifest

log = logging.getLogger(__name__)

# A .fit file crosses the wire base64-encoded inside a JSON frame. The server
# already caps an upload at MAX_UPLOAD_BYTES (50 MiB); apply the same ceiling
# to what a connector may hand us, so a compromised or buggy client cannot
# make the server hold an arbitrary amount in memory.
MAX_ACTIVITY_BYTES = 50 * 1024 * 1024

# Listing and reading are the slow ones - a first scan of a long Zwift history
# transfers hundreds of files - so they get their own, longer budgets.
_LIST_TIMEOUT_S = 120.0
_READ_TIMEOUT_S = 120.0


class RemoteBackend(Backend):
    """Talks to the connector attached for one user."""

    name = "remote"

    def __init__(self, user_id: Optional[int]) -> None:
        self.user_id = user_id

    def _call(self, method: str, params: Optional[dict] = None, **kw):
        return connectorhub.require(self.user_id).call_sync(method, params, **kw)

    # ----------------------------------------------------- discovery

    def activity_candidates(self) -> List[dict]:
        rows = self._call("paths.activity_candidates") or []
        return [
            {"path": str(r.get("path", "")), "exists": bool(r.get("exists"))}
            for r in rows
            if isinstance(r, dict)
        ]

    def zwift_id_candidates(self) -> List[dict]:
        rows = self._call("paths.zwift_id_candidates") or []
        return [
            {
                "zwift_id": str(r.get("zwift_id", "")),
                "path": str(r.get("path", "")),
                "mtime": float(r.get("mtime") or 0.0),
            }
            for r in rows
            if isinstance(r, dict)
        ]

    def default_activities_dir(self) -> Optional[str]:
        return self._call("paths.default_activities_dir")

    def workouts_root(self) -> Optional[str]:
        return self._call("paths.workouts_root")

    def resolve_export_dir(
        self, zwift_id: Optional[str] = None, override: Optional[str] = None
    ) -> Tuple[Optional[str], str]:
        result = self._call(
            "paths.resolve_export_dir",
            {"zwift_id": zwift_id, "override": override},
        ) or {}
        return result.get("directory"), str(result.get("reason") or "missing")

    def validate_dir(self, value: str) -> Tuple[Optional[str], Optional[str]]:
        # Runs on the connector: these are its folders, and measuring them
        # against the server's trusted roots would reject every valid answer.
        result = self._call("paths.validate_dir", {"value": value}) or {}
        return result.get("clean"), result.get("error")

    # ---------------------------------------------- activity files

    def list_activities(self, directory: Optional[str] = None) -> ActivityListing:
        result = self._call(
            "activities.list", {"directory": directory}, timeout=_LIST_TIMEOUT_S
        ) or {}
        files = []
        for row in result.get("files") or []:
            if not isinstance(row, dict):
                continue
            path = str(row.get("path") or "")
            if not path:
                continue
            files.append(
                ActivityFile(
                    path=path,
                    # Trust the connector for the name but never for its shape:
                    # it becomes the activity's filename column, and a value
                    # with a separator in it would misrepresent provenance.
                    name=os.path.basename(str(row.get("name") or path)),
                    mtime=float(row.get("mtime") or 0.0),
                    size=int(row.get("size") or 0),
                )
            )
        return ActivityListing(
            directory=result.get("directory"),
            exists=bool(result.get("exists")),
            files=files,
            skipped=int(result.get("skipped") or 0),
        )

    @contextmanager
    def readable_activity(self, path: str) -> Iterator[str]:
        """Fetch the file's bytes and hand the parser a temporary local copy."""
        result = self._call(
            "activities.read", {"path": path}, timeout=_READ_TIMEOUT_S
        ) or {}
        encoded = result.get("content")
        if not isinstance(encoded, str):
            raise RpcError(f"connector returned no content for {path}")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RpcError(f"connector sent unreadable content for {path}") from exc
        if len(content) > MAX_ACTIVITY_BYTES:
            raise RpcError(
                f"{path} is {len(content)} bytes, over the "
                f"{MAX_ACTIVITY_BYTES} limit"
            )

        # Suffix from the *remote* basename, so the parser sees a .fit. The
        # temp file lives on the server and is deleted whatever happens.
        suffix = os.path.splitext(os.path.basename(path))[1] or ".fit"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            tmp.write(content)
            tmp.flush()
            tmp.close()
            yield tmp.name
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                log.debug("could not remove temp copy of %s", path, exc_info=True)

    # ----------------------------------------------- workout files

    def apply_exports(self, manifest: ExportManifest) -> dict:
        result = self._call(
            "workouts.sync",
            {
                "zwift_id": manifest.zwift_id,
                "override": manifest.override,
                "write": manifest.write,
                "remove": manifest.remove,
                "require_existing": manifest.require_existing,
                "resolution": manifest.resolution,
            },
            timeout=_LIST_TIMEOUT_S,
        ) or {}
        return {
            "status": str(result.get("status") or "missing"),
            "directory": result.get("directory"),
            "exported": int(result.get("exported") or 0),
            "removed": int(result.get("removed") or 0),
            "reason": result.get("reason"),
            "paths": [str(p) for p in (result.get("paths") or [])],
        }


def get_remote_backend(user_id: Optional[int]) -> RemoteBackend:
    """A backend bound to one user's connector.

    Cheap and stateless - the live socket lives in ``connectorhub``, looked up
    per call, so a backend handed out before a connector attached starts
    working the moment it does.
    """
    return RemoteBackend(user_id)


__all__ = ["RemoteBackend", "get_remote_backend", "ConnectorUnavailable"]
