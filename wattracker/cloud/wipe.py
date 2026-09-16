"""Delete one rider's cloud data, and everything that could read it (#170).

This is the only operation in the package that destroys a rider's data, and it
is deliberately not reachable from the sync client.  The sync identity's
storage roles carry no delete action at all (``infra/azure/main.bicep``), so a
compromised writer cannot empty a scope; deletion is a separate, privileged
path run by an operator identity that holds delete and nothing else.

What a wipe removes, in the order it removes it
===============================================

The order is the design, not an implementation detail.  **Credentials first,
data last.**

1. ``writer`` rows for the scope -- the desktop's sync credential.
2. ``device`` rows for the scope, and the ``device-seen`` row each one owns.
3. ``context`` rows for the scope, and the ``context-index`` row each one owns.
4. ``invitation`` and ``device-pairing`` rows for the scope -- anything that
   could still be redeemed into a credential for it.
5. Only then the objects, their blobs, the batch idempotency markers and the
   scope's revision row, through :meth:`TenantStore.purge_scope`.

Taking access away before taking the data away means a sync in flight cannot
write a fresh object into a scope that has already been emptied, and an
invitation cannot be redeemed into a credential for a scope that is about to
stop existing.  It also decides what a *partial* wipe looks like: if this
fails halfway, what is left over is data nobody holds a credential for, never
a live credential pointing at data that is half gone.

What a wipe deliberately keeps
==============================

* **The budget kill switch.**  ``KILL_SWITCH_RECORD_KIND`` is deployment-wide
  and an absent row reads as *ENABLED*, so deleting it would silently
  re-enable spending and the public API on a deployment somebody deliberately
  killed -- no error, no log, and a wipe is most likely to be run exactly when
  the switch is most likely to be thrown.  Two things stop that here.  The row
  now lives in a table of its own (``CloudControl``), which the operator wipe
  role is not scoped to, so the grant cannot reach it; and this module
  excludes the kind by name anyway, in :data:`WIPE_PROTECTED_RECORD_KINDS`,
  because a wipe that depends on a scope filter missing a row is a wipe whose
  safety is an emergent property of the filter this issue changes.

* **The daily quota counters.**  ``quota-counter`` rows *are* keyed by
  ``(namespace, scope)``, so unlike the kill switch a scope filter would find
  them.  They are kept, for the same shape of reason: an absent counter row
  reads as *zero*, so wiping them hands the scope a fresh daily allowance, and
  wipe-then-re-enrol becomes a way to buy an unlimited budget on a deployment
  whose entire cost control is those counters.  They cost nothing to keep --
  a row is a byte count, a request count and a UTC date, with no heart rate,
  no weight and no ride in it -- and each one is reclaimed in place by the
  first charge of the next day, so the table does not grow.  The installation
  counters would be worse still: they are shared with the *other* rider, so
  deleting them would refund a stranger's spending.

* **Replay claims.**  A ``nonce`` row is keyed by a digest of the namespace,
  credential id and nonce with nothing recoverable in the payload, so there is
  no way to select one scope's claims; and there is no reason to want to.
  Deleting an unexpired claim re-opens a captured request to replay, which is
  strictly worse than leaving a row that expires within 600 seconds, grants
  nothing, and identifies nobody.

* **Every other scope.**  Every row is matched on the *payload's* namespace
  and local scope with a constant-time comparison, not on a prefix, a
  substring or the row key.  A row whose payload will not decode is left where
  it is and counted in ``skipped``: a row nobody can read is a row nobody can
  prove belongs to this rider.

The tombstone decision
======================

**A wipe is irreversible and writes no tombstone.**  ``RECOVERY_RETENTION``
(``storage.py``) keeps a *tombstoned* object recoverable for seven days, and
that window is right for what it was built for -- an ordinary sync deletion of
one ride the rider wants back.  It is wrong here.  Honouring it would mean
that "delete everything I have synced" leaves every byte of heart rate, body
weight and ride history in the store for a week, still readable through
``include_deleted=True``, and restorable by ``recover_deleted`` -- which is
precisely what the rider asked to be rid of.  So the wipe purges outright.

Because it is irreversible, it must be asked for in those words:
:func:`wipe_scope` takes a required keyword-only ``irreversible`` and refuses
anything but ``True``.  There is no default, so no caller can perform a wipe
without having typed the word.

One copy does outlive this call and it is not ours: the storage account sets a
7-day blob soft-delete window (``deleteRetentionPolicy`` in
``infra/azure/main.bicep``).  Those copies are reachable only by an Azure
account owner through the storage platform, never by this application, any
credential it issues, or any identity it deploys.  That is documented rather
than defeated -- turning it off to make this function's promise tidier would
remove the deployment's only protection against an accidental purge.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Protocol

# The private helpers are imported on purpose.  ``_digest_token`` is the
# function that *addresses* every row in the auth table, and
# ``_require_namespace`` / ``_require_local_scope`` are the validators every
# other path applies to these two arguments.  Re-implementing any of them here
# would create a second source of truth for where a row lives, and a wipe that
# computes a row key differently from the code that wrote it is a wipe that
# either misses rows or reaches rows it was not asked about.
from .security import (
    DEVICE_SEEN_RECORD_KIND,
    _digest_token,
    _require_local_scope,
    _require_namespace,
)
from .storage import ScopePurge

_log = logging.getLogger(__name__)


#: The record kinds a wipe walks.  Each one stores ``namespace`` and
#: ``local_user_scope`` in its payload, which is what makes it selectable for
#: one rider at all.  This is an allowlist in the same sense as
#: ``SWEEPABLE_RECORD_KINDS``: a kind is touched because it is named here, not
#: because a filter happened to match it.
WIPE_RECORD_KINDS: Final[tuple[str, ...]] = (
    "writer",
    "device",
    "context",
    "invitation",
    "device-pairing",
)

#: Restated by name, and checked again at call time.  ``kill-switch`` and
#: ``quota-counter`` are the two rows whose *absence* is read as a permissive
#: value -- enabled, and zero-spent -- so deleting either fails open and does
#: so silently.  ``health`` is the startup access probe and belongs to the
#: deployment, not to a rider.  ``nonce`` is unselectable by scope and deleting
#: an unexpired one would re-open a replay.
WIPE_PROTECTED_RECORD_KINDS: Final[frozenset[str]] = frozenset({
    "kill-switch",
    "quota-counter",
    "health",
    "nonce",
})

if set(WIPE_RECORD_KINDS) & WIPE_PROTECTED_RECORD_KINDS:  # pragma: no cover
    raise RuntimeError("a wiped record kind is also marked protected")

#: One pass per kind, bounded like every other enumeration in this package.
#: ``iter_records`` has no cursor, so a pass that comes back full may not have
#: seen everything; :attr:`ScopeWipeReport.complete` reports that rather than
#: looping, and a second call finishes the job.
WIPE_SCAN_LIMIT: Final = 10_000


class TenantStore(Protocol):
    """The one storage capability a wipe needs."""

    def purge_scope(self, namespace: str, local_user_scope: str) -> ScopePurge: ...


class StateBackend(Protocol):
    """The subset of ``SecurityStateBackend`` a wipe needs."""

    def delete(self, kind: str, key: str) -> bool: ...

    def iter_records(
        self, kind: str, *, limit: int
    ) -> list[tuple[str, dict[str, Any] | None]]: ...


@dataclass(frozen=True)
class ScopeWipeReport:
    """What one wipe removed, kept, and could not decide about.

    ``complete`` is False when any pass filled its scan bound, so the operator
    learns "run it again" from the return value rather than from a silently
    short result.  ``skipped`` counts rows left alone because their payload
    would not decode or did not name a scope -- never a guess.
    """

    namespace: str
    local_user_scope: str
    records: Mapping[str, int] = field(default_factory=dict)
    objects: int = 0
    blobs: int = 0
    markers: int = 0
    skipped: int = 0
    complete: bool = True

    @property
    def credentials(self) -> int:
        """Writer and device rows removed; what "no credential" is counted by."""

        return self.records.get("writer", 0) + self.records.get("device", 0)

    @property
    def total_records(self) -> int:
        return sum(self.records.values())


def _matches_scope(
    value: object, namespace: str, local_user_scope: str
) -> bool:
    """Whether a stored payload names exactly this scope.

    Compared in constant time and on equality of the whole field, never a
    prefix: ``"rider"`` must not match ``"rider2"``, and a namespace is a
    digest an attacker would otherwise get to probe one character at a time.
    A payload missing either field is not a match -- it is not evidence of
    anything, and a wipe acts only on positive evidence.

    ``compare_digest`` refuses non-ASCII text, and a row edited outside
    ``_require_local_scope`` could hold some.  That is reported as "not this
    rider" rather than allowed to raise: a wipe that dies partway through has
    already removed credentials, and one unparseable row must not be able to
    stop the rest of a rider's data being deleted.
    """

    if not isinstance(value, Mapping):
        return False
    stored_namespace = value.get("namespace")
    stored_scope = value.get("local_user_scope")
    if not isinstance(stored_namespace, str) or not isinstance(stored_scope, str):
        return False
    try:
        return hmac.compare_digest(
            stored_namespace, namespace
        ) and hmac.compare_digest(stored_scope, local_user_scope)
    except TypeError:
        return False


def _owned_row_key(value: Mapping[str, Any], field_name: str) -> str | None:
    """The row key of a companion row this record owns, or ``None``.

    ``device-seen`` is addressed by ``sha256(credential_id)`` and
    ``context-index`` by ``sha256(context_id)`` -- the same digest that
    addresses the row being deleted.  Deriving the companion's key from the
    parent payload is what keeps those two kinds in scope without enumerating
    them: neither carries a namespace of its own, so neither could be matched
    on one, and walking them blind is exactly how a wipe would reach into
    another rider's rows.
    """

    identifier = value.get(field_name)
    if not isinstance(identifier, str) or not identifier:
        return None
    try:
        return _digest_token(identifier).hex()
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def wipe_scope(
    namespace: str,
    local_user_scope: str,
    *,
    irreversible: bool,
    store: TenantStore | None = None,
    security_backend: StateBackend | None = None,
    scan_limit: int = WIPE_SCAN_LIMIT,
) -> ScopeWipeReport:
    """Remove every object, credential and device registration for one scope.

    This is the library entry point the operator CLI (#169) and, after #305,
    the operator HTTP route will call.  It takes an already-verified
    ``(namespace, local_user_scope)``: the namespace is a server-derived
    digest and is never a caller's claim, exactly as in every other path here.

    ``irreversible=True`` is required and is not a formality -- see the module
    docstring.  Nothing this removes can be recovered through this
    application.

    Either collaborator may be ``None`` so the two halves can be exercised
    apart; passing neither is a no-op and is refused.
    """

    if irreversible is not True:
        raise ValueError(
            "a scope wipe is irreversible and must be requested with "
            "irreversible=True"
        )
    namespace_text = _require_namespace(namespace)
    scope_text = _require_local_scope(local_user_scope)
    if store is None and security_backend is None:
        raise ValueError("a scope wipe needs a store, a state backend, or both")
    if isinstance(scan_limit, bool) or not isinstance(scan_limit, int) or scan_limit < 1:
        raise ValueError("scan_limit must be positive")

    records: dict[str, int] = {}
    skipped = 0
    complete = True

    if security_backend is not None:
        backend = security_backend

        def _delete(kind: str, key: str) -> None:
            # Re-asserted here rather than trusted from the allowlist above:
            # this is the check that has to hold even if someone edits
            # WIPE_RECORD_KINDS or adds a companion kind below.
            if kind in WIPE_PROTECTED_RECORD_KINDS:  # pragma: no cover - guard
                raise RuntimeError(f"a scope wipe must never delete {kind!r} rows")
            if backend.delete(kind, key):
                records[kind] = records.get(kind, 0) + 1

        for kind in WIPE_RECORD_KINDS:
            if kind in WIPE_PROTECTED_RECORD_KINDS:  # pragma: no cover - guard
                continue
            rows = security_backend.iter_records(kind, limit=scan_limit)
            if len(rows) >= scan_limit:
                complete = False
            for key, value in rows:
                if not isinstance(value, Mapping):
                    # An unreadable row is not this rider's until it says so.
                    skipped += 1
                    continue
                if not _matches_scope(value, namespace_text, scope_text):
                    continue
                if kind == "device":
                    companion = _owned_row_key(value, "credential_id")
                    if companion is not None:
                        _delete(DEVICE_SEEN_RECORD_KIND, companion)
                elif kind == "context":
                    companion = _owned_row_key(value, "context_id")
                    if companion is not None:
                        _delete("context-index", companion)
                _delete(kind, key)

    purge = ScopePurge()
    if store is not None:
        # Last, and only after the credentials are gone: see the module note
        # on ordering.
        purge = store.purge_scope(namespace_text, scope_text)

    report = ScopeWipeReport(
        namespace=namespace_text,
        local_user_scope=scope_text,
        records=dict(records),
        objects=purge.objects,
        blobs=purge.blobs,
        markers=purge.markers,
        skipped=skipped + purge.skipped,
        complete=complete,
    )
    _log.info(
        "cloud scope wipe removed %d records and %d objects (complete=%s)",
        report.total_records,
        report.objects,
        report.complete,
    )
    return report


__all__ = [
    "ScopeWipeReport",
    "WIPE_PROTECTED_RECORD_KINDS",
    "WIPE_RECORD_KINDS",
    "WIPE_SCAN_LIMIT",
    "wipe_scope",
]
