"""Bounded wire models for the cloud API.

The cloud service accepts records, never paths or storage instructions.  The
models deliberately discard installation and local-user selectors supplied by
clients; those values come from the verified credential at the API boundary.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

MAX_BATCH_OBJECTS = 1_000
MAX_OBJECT_ID_LENGTH = 128
MAX_KIND_LENGTH = 64
MAX_BATCH_ID_LENGTH = 128
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_BATCH_REVISION = (1 << 63) - 1
MAX_PAYLOAD_ARRAY_ITEMS = 16_384

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DANGEROUS_KEYS = {
    "account_id",
    "azure_account",
    "blob_path",
    "command",
    "file",
    "filename",
    "installation_id",
    "installation",
    "local_user_id",
    "local_user_scope",
    "namespace",
    "path",
    "partition_key",
    "sas",
    "sas_token",
    "storage_account",
    "storage_url",
    "tenant",
    "table_partition_key",
    "url",
    "user_id",
    "user_name",
    "username",
}
_DANGEROUS_KEY_NORMALIZED = {
    re.sub(r"[^a-z0-9]", "", value) for value in _DANGEROUS_KEYS
}


class ModelError(ValueError):
    """The caller supplied a record outside the bounded sync schema."""


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ModelError(f"invalid {label}")
    return value


def _positive_revision(value: Any, label: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"invalid {label}")
    if value < 1 or value > MAX_BATCH_REVISION:
        raise ModelError(f"invalid {label}")
    return value


def _walk_payload(value: Any, *, key: Optional[str] = None, depth: int = 0) -> None:
    if depth > 32:
        raise ModelError("payload nesting is too deep")
    if (
        key is not None
        and (
            key.lower() in _DANGEROUS_KEYS
            or re.sub(r"[^a-z0-9]", "", key.lower()) in _DANGEROUS_KEY_NORMALIZED
        )
    ):
        raise ModelError(f"unsupported payload field: {key}")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 32_768:
            raise ModelError("payload string is too large")
        return
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ModelError("payload object has too many fields")
        for child_key, child_value in value.items():
            if not isinstance(child_key, str) or len(child_key) > 128:
                raise ModelError("invalid payload field name")
            _walk_payload(child_value, key=child_key, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if len(value) > MAX_PAYLOAD_ARRAY_ITEMS:
            raise ModelError("payload array is too large")
        for child in value:
            _walk_payload(child, depth=depth + 1)
        return
    raise ModelError("payload contains an unsupported value")


@dataclass(frozen=True)
class CloudObject:
    """One versioned object in a single bound local-user scope."""

    object_id: str
    kind: str
    revision: int
    data: Mapping[str, Any]
    deleted: bool = False

    def __post_init__(self) -> None:
        object_id = _opaque(self.object_id, "object id")
        if len(object_id) > MAX_OBJECT_ID_LENGTH:
            raise ModelError("object id is too long")
        if not isinstance(self.kind, str) or not _KIND.fullmatch(self.kind):
            raise ModelError("invalid object kind")
        _positive_revision(self.revision, "object revision")
        if not isinstance(self.data, Mapping):
            raise ModelError("object data must be an object")
        _walk_payload(self.data)
        try:
            encoded = json.dumps(self.data, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise ModelError("object data is not JSON") from exc
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ModelError("object data is too large")
        if not isinstance(self.deleted, bool):
            raise ModelError("invalid deletion marker")

    def wire(self, *, include_deleted: bool = True) -> dict:
        result = {
            "id": self.object_id,
            "kind": self.kind,
            "revision": self.revision,
            "data": dict(self.data),
        }
        if include_deleted and self.deleted:
            result["deleted"] = True
        return result


@dataclass(frozen=True)
class SyncBatch:
    """A bounded, idempotent batch.

    ``installation_id`` and ``local_user_scope`` are retained only while
    parsing so callers can prove they are ignored by the API.  They are never
    used for storage authorization or returned by the service.
    """

    batch_id: str
    revision: int
    objects: tuple[CloudObject, ...]
    supplied_installation_id: Optional[str] = None
    supplied_local_user_scope: Optional[str] = None

    def __post_init__(self) -> None:
        self_batch_id = _opaque(self.batch_id, "batch id")
        if len(self_batch_id) > MAX_BATCH_ID_LENGTH:
            raise ModelError("batch id is too long")
        _positive_revision(self.revision)
        if not isinstance(self.objects, tuple):
            raise ModelError("objects must be a tuple")
        if not self.objects or len(self.objects) > MAX_BATCH_OBJECTS:
            raise ModelError("invalid object count")
        if any(not isinstance(obj, CloudObject) for obj in self.objects):
            raise ModelError("invalid object")
        if len({obj.object_id for obj in self.objects}) != len(self.objects):
            raise ModelError("duplicate object id")

    @classmethod
    def from_wire(cls, value: Any) -> "SyncBatch":
        if not isinstance(value, Mapping):
            raise ModelError("batch must be an object")
        batch_id = _opaque(value.get("batch_id"), "batch id")
        if len(batch_id) > MAX_BATCH_ID_LENGTH:
            raise ModelError("batch id is too long")
        revision = _positive_revision(value.get("revision"))
        raw_objects = value.get("objects")
        if not isinstance(raw_objects, Sequence) or isinstance(
            raw_objects, (str, bytes, bytearray)
        ):
            raise ModelError("objects must be an array")
        if not raw_objects or len(raw_objects) > MAX_BATCH_OBJECTS:
            raise ModelError("invalid object count")
        objects = []
        for raw in raw_objects:
            if not isinstance(raw, Mapping):
                raise ModelError("object must be an object")
            objects.append(
                CloudObject(
                    object_id=raw.get("id"),
                    kind=raw.get("kind"),
                    revision=raw.get("revision"),
                    data=raw.get("data", {}),
                    deleted=raw.get("deleted", False),
                )
            )
        if len({obj.object_id for obj in objects}) != len(objects):
            raise ModelError("duplicate object id")
        return cls(
            batch_id=batch_id,
            revision=revision,
            objects=tuple(objects),
            supplied_installation_id=(
                value.get("installation_id")
                if isinstance(value.get("installation_id"), str)
                else None
            ),
            supplied_local_user_scope=(
                value.get("local_user_scope")
                if isinstance(value.get("local_user_scope"), str)
                else None
            ),
        )

    def digest_material(self) -> bytes:
        return json.dumps(
            {
                "batch_id": self.batch_id,
                "revision": self.revision,
                "objects": [obj.wire() for obj in self.objects],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
