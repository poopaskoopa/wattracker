"""Durable, scope-bound queue for mobile RPE edit intents."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable, Mapping

from .security import SecurityStateBackend, SecurityStateUnavailable


EDIT_QUEUE_KIND = "edit-queue"
_KEY_DOMAIN = b"wattracker-cloud-edit-queue-v1\0"
_PENDING_TTL = 14 * 24 * 60 * 60
_SETTLED_TTL = 7 * 24 * 60 * 60
_PENDING_LIMIT = 50
_SETTLED_LIMIT = 100
_PAYLOAD_LIMIT = 32_000
_EDIT_ID = re.compile(r"[0-9a-f]{32}\Z")
_SETTLED_STATUSES = frozenset({"applied", "conflict", "rejected", "expired"})


class EditQueueFull(ValueError):
    """An edit would exceed a bounded queue or serialized payload limit."""


def edit_queue_key(namespace: str, local_user_scope: str) -> str:
    parts = (namespace, local_user_scope)
    if any(not isinstance(part, str) or "\0" in part for part in parts):
        raise ValueError("edit queue key parts must be NUL-free text")
    return hashlib.sha256(
        _KEY_DOMAIN + namespace.encode("utf-8") + b"\0" + local_user_scope.encode("utf-8")
    ).hexdigest()


def _empty(namespace: str, scope: str) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "local_user_scope": scope,
        "pending": [],
        "settled": [],
    }


def _serialized_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _pruned(
    value: Mapping[str, Any] | None,
    namespace: str,
    scope: str,
    now: float,
) -> dict[str, Any]:
    if value is None:
        queue = _empty(namespace, scope)
    else:
        if (
            value.get("namespace") != namespace
            or value.get("local_user_scope") != scope
            or not isinstance(value.get("pending"), list)
            or not isinstance(value.get("settled"), list)
        ):
            raise SecurityStateUnavailable("edit queue record is malformed")
        queue = {
            "namespace": namespace,
            "local_user_scope": scope,
            "pending": [dict(item) for item in value["pending"] if isinstance(item, Mapping)],
            "settled": [dict(item) for item in value["settled"] if isinstance(item, Mapping)],
        }
        if len(queue["pending"]) != len(value["pending"]) or len(queue["settled"]) != len(value["settled"]):
            raise SecurityStateUnavailable("edit queue record is malformed")
    for item in queue["pending"]:
        created = item.get("created_at")
        if (
            set(item) != {"edit_id", "activity_id", "rpe", "base_rpe", "submitted_by", "created_at"}
            or
            not isinstance(item.get("edit_id"), str)
            or _EDIT_ID.fullmatch(item["edit_id"]) is None
            or not isinstance(item.get("submitted_by"), str)
            or isinstance(item.get("activity_id"), bool)
            or not isinstance(item.get("activity_id"), int)
            or not 1 <= item["activity_id"] <= 2**53 - 1
            or isinstance(item.get("rpe"), bool)
            or not isinstance(item.get("rpe"), int)
            or not 1 <= item["rpe"] <= 10
            or (item.get("base_rpe") is not None and (
                isinstance(item.get("base_rpe"), bool)
                or not isinstance(item.get("base_rpe"), int)
                or not 1 <= item["base_rpe"] <= 10
            ))
            or isinstance(created, bool)
            or not isinstance(created, (int, float))
        ):
            raise SecurityStateUnavailable("edit queue record is malformed")
    for item in queue["settled"]:
        settled_at = item.get("settled_at")
        if (
            set(item) != {"edit_id", "submitted_by", "activity_id", "rpe", "status", "settled_at"}
            or
            not isinstance(item.get("edit_id"), str)
            or _EDIT_ID.fullmatch(item["edit_id"]) is None
            or not isinstance(item.get("submitted_by"), str)
            or isinstance(item.get("activity_id"), bool)
            or not isinstance(item.get("activity_id"), int)
            or not 1 <= item["activity_id"] <= 2**53 - 1
            or isinstance(item.get("rpe"), bool)
            or not isinstance(item.get("rpe"), int)
            or not 1 <= item["rpe"] <= 10
            or not isinstance(item.get("status"), str)
            or item["status"] not in _SETTLED_STATUSES
            or isinstance(settled_at, bool)
            or not isinstance(settled_at, (int, float))
        ):
            raise SecurityStateUnavailable("edit queue record is malformed")
    expiry = now - _PENDING_TTL
    still_pending = []
    newly_settled = []
    for item in queue["pending"]:
        created = item.get("created_at")
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            raise SecurityStateUnavailable("edit queue record is malformed")
        if float(created) < expiry:
            newly_settled.append({
                "edit_id": item["edit_id"],
                "submitted_by": item["submitted_by"],
                "activity_id": item["activity_id"],
                "rpe": item["rpe"],
                "status": "expired",
                "settled_at": now,
            })
        else:
            still_pending.append(item)
    settled = queue["settled"] + newly_settled
    settled = [
        item for item in settled
        if isinstance(item.get("settled_at"), (int, float))
        and not isinstance(item.get("settled_at"), bool)
        and float(item["settled_at"]) >= now - _SETTLED_TTL
    ][-_SETTLED_LIMIT:]
    queue["pending"] = still_pending
    queue["settled"] = settled
    return queue


class EditQueue:
    """Apply all queue mutations through the backend's etag compare-and-set."""

    def __init__(
        self,
        backend: SecurityStateBackend,
        *,
        clock: Callable[[], float] = time.time,
        resolve_device: Callable[[str], Any] | None = None,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._resolve_device = resolve_device

    def _update(self, namespace: str, scope: str, transform: Callable[[dict[str, Any], float], None]) -> dict[str, Any]:
        key = edit_queue_key(namespace, scope)

        def apply(current: Mapping[str, Any] | None) -> Mapping[str, Any]:
            now = float(self._clock())
            queue = _pruned(current, namespace, scope, now)
            transform(queue, now)
            queue["settled"] = queue["settled"][-_SETTLED_LIMIT:]
            if len(queue["pending"]) > _PENDING_LIMIT or len(queue["settled"]) > _SETTLED_LIMIT:
                raise EditQueueFull("edit queue full")
            if _serialized_size(queue) > _PAYLOAD_LIMIT:
                raise EditQueueFull("edit queue full")
            return queue

        try:
            return self._backend.update(EDIT_QUEUE_KIND, key, apply)
        except (EditQueueFull, SecurityStateUnavailable):
            raise
        except Exception as exc:
            raise SecurityStateUnavailable("edit queue unavailable") from exc

    def submit(
        self,
        namespace: str,
        scope: str,
        *,
        edit_id: str,
        activity_id: int,
        rpe: int,
        base_rpe: int | None,
        submitted_by: str,
    ) -> tuple[str, float | None]:
        result: dict[str, Any] = {}

        def transform(queue: dict[str, Any], now: float) -> None:
            result.clear()
            pending = queue["pending"]
            settled = queue["settled"]
            existing = next((item for item in pending if item.get("edit_id") == edit_id), None)
            if existing is not None:
                same = all(existing.get(field) == value for field, value in (
                    ("activity_id", activity_id), ("rpe", rpe),
                    ("base_rpe", base_rpe),
                ))
                result.update(status="pending" if same else "conflict", expires_at=(float(existing["created_at"]) + _PENDING_TTL if same else None))
                return
            previous = next((item for item in settled if item.get("edit_id") == edit_id), None)
            if previous is not None:
                result.update(status=previous["status"], expires_at=None)
                return
            same_activity = next((item for item in pending if item.get("activity_id") == activity_id), None)
            if same_activity is not None:
                pending.remove(same_activity)
                settled.append({
                    "edit_id": same_activity["edit_id"],
                    "submitted_by": same_activity["submitted_by"],
                    "activity_id": same_activity["activity_id"],
                    "rpe": same_activity["rpe"],
                    "status": "rejected",
                    "settled_at": now,
                })
            elif len(pending) >= _PENDING_LIMIT:
                raise EditQueueFull("edit queue full")
            pending.append({
                "edit_id": edit_id,
                "activity_id": activity_id,
                "rpe": rpe,
                "base_rpe": base_rpe,
                "submitted_by": submitted_by,
                "created_at": now,
            })
            result.update(status="pending", expires_at=now + _PENDING_TTL)

        self._update(namespace, scope, transform)
        return result["status"], result.get("expires_at")

    def status(self, namespace: str, scope: str, submitted_by: str) -> list[dict[str, Any]]:
        queue = self._update(namespace, scope, lambda _queue, _now: None)
        rows = []
        for item in queue["pending"]:
            if item["submitted_by"] == submitted_by:
                rows.append({
                    "edit_id": item["edit_id"], "activity_id": item["activity_id"],
                    "rpe": item["rpe"], "status": "pending", "settled_at": None,
                })
        for item in queue["settled"]:
            if item["submitted_by"] == submitted_by:
                rows.append({
                    "edit_id": item["edit_id"], "activity_id": item["activity_id"],
                    "rpe": item["rpe"], "status": item["status"],
                    "settled_at": item["settled_at"],
                })
        return rows[:_PENDING_LIMIT + _SETTLED_LIMIT]

    def pull(self, namespace: str, scope: str) -> list[dict[str, Any]]:
        key = edit_queue_key(namespace, scope)
        try:
            if self._backend.read(EDIT_QUEUE_KIND, key) is None:
                return []
        except Exception as exc:
            raise SecurityStateUnavailable("edit queue unavailable") from exc
        result: list[dict[str, Any]] = []

        def transform(queue: dict[str, Any], now: float) -> None:
            result.clear()
            kept = []
            resolved: dict[str, Any] = {}
            for item in queue["pending"]:
                submitter = item["submitted_by"]
                if submitter not in resolved:
                    resolved[submitter] = self._resolve_device(submitter) if self._resolve_device else None
                device = resolved[submitter]
                if (
                    device is None
                    or device.namespace != namespace
                    or device.local_user_scope != scope
                    or not device.has_capability("annotate")
                ):
                    queue["settled"].append({
                        "edit_id": item["edit_id"], "submitted_by": item["submitted_by"],
                        "activity_id": item["activity_id"], "rpe": item["rpe"],
                        "status": "rejected", "settled_at": now,
                    })
                else:
                    kept.append(item)
            queue["pending"] = kept
            result.extend(sorted(kept, key=lambda item: (item["created_at"], item["edit_id"]))[:_PENDING_LIMIT])

        self._update(namespace, scope, transform)
        return [{field: item[field] for field in ("edit_id", "activity_id", "rpe", "base_rpe", "created_at")} for item in result]

    def ack(self, namespace: str, scope: str, results: list[dict[str, str]]) -> int:
        key = edit_queue_key(namespace, scope)
        try:
            if self._backend.read(EDIT_QUEUE_KIND, key) is None:
                return 0
        except Exception as exc:
            raise SecurityStateUnavailable("edit queue unavailable") from exc
        acked = 0
        by_id = {item["edit_id"]: item["status"] for item in results}

        def transform(queue: dict[str, Any], now: float) -> None:
            nonlocal acked
            acked = 0
            pending = []
            for item in queue["pending"]:
                status = by_id.get(item["edit_id"])
                if status is None:
                    pending.append(item)
                    continue
                queue["settled"].append({
                    "edit_id": item["edit_id"], "submitted_by": item["submitted_by"],
                    "activity_id": item["activity_id"], "rpe": item["rpe"],
                    "status": status, "settled_at": now,
                })
                acked += 1
            queue["pending"] = pending

        self._update(namespace, scope, transform)
        return acked

    def drop_submitter(self, namespace: str, scope: str, submitted_by: str) -> int:
        key = edit_queue_key(namespace, scope)
        try:
            if self._backend.read(EDIT_QUEUE_KIND, key) is None:
                return 0
        except Exception as exc:
            raise SecurityStateUnavailable("edit queue unavailable") from exc
        removed = 0

        def transform(queue: dict[str, Any], _now: float) -> None:
            nonlocal removed
            removed = 0
            before = len(queue["pending"])
            queue["pending"] = [item for item in queue["pending"] if item["submitted_by"] != submitted_by]
            removed = before - len(queue["pending"])

        self._update(namespace, scope, transform)
        return removed


def drop_revoked_submitter(
    backend: SecurityStateBackend,
    namespace: str,
    scope: str,
    submitted_by: str,
    *,
    clock: Callable[[], float] = time.time,
) -> int:
    return EditQueue(backend, clock=clock).drop_submitter(namespace, scope, submitted_by)
