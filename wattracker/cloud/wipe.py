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

Companion rows, and why a payload field is not an address
=========================================================

``device-seen`` and ``context-index`` carry no namespace and no local scope,
so neither can be matched on one; each is reachable only through the row that
owns it.  That makes the *derivation* of the companion's key the only access
control there is, and it therefore cannot be a bare read of a payload field.
The threat model includes a compromised read plane, which holds
``entities/write`` on ``authTable`` (``infra/azure/main.bicep``) and can
rewrite any row's payload; a ``credential_id`` or ``context_id`` edited to
name another rider's row turned one rider's wipe into a deletion of theirs.

So each companion key is *proved* against the record claiming it, and a
record that cannot prove one has its companion left alone and counted in
``skipped`` -- never deleted on the strength of an editable field:

* ``device`` re-derives ``sha256(credential_id)`` and requires it to equal
  the device row's own key, through ``security._device_id_from_value`` -- the
  same check the device listing already applies, not a second one.
* ``context`` cannot do that (its key digests the token, its companion's key
  digests the context id), so the index is read back and required to name
  this very context row in its ``token_digest``.

The record itself still goes.  A tampered payload does not make a row stop
being a credential: both ``_find_device_locked`` and ``read_context_token``
resolve from the row's own address and never read the edited field, so
leaving one behind would leave a live credential for a scope the rider asked
to empty.  Only the reach into a row this record has not proved it owns is
withheld.

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
    _device_id_from_value,
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
    """The subset of ``SecurityStateBackend`` a wipe needs.

    ``read`` is here for one reason: a ``context`` row cannot prove which
    ``context-index`` row belongs to it without reading that row back.  See
    :func:`_context_index_companion`.
    """

    def read(self, kind: str, key: str) -> dict[str, Any] | None: ...

    def delete(self, kind: str, key: str) -> bool: ...

    def iter_records(
        self, kind: str, *, limit: int
    ) -> list[tuple[str, dict[str, Any] | None]]: ...


@dataclass(frozen=True)
class ScopeWipeReport:
    """What one wipe removed, kept, and could not decide about.

    ``complete`` is False when any pass filled its scan bound, so the operator
    learns "run it again" from the return value rather than from a silently
    short result.

    ``orphans`` comes straight from
    :attr:`~wattracker.cloud.storage.ScopePurge.orphans`: blobs that were
    under the scope's blob prefix with no row naming them, found and deleted
    by the store's container pass.  It is normally zero.  A non-zero value is
    not an error and not a partial wipe -- the data is gone either way -- but
    it is evidence that an earlier sync was interrupted between writing a blob
    and writing its row, which is worth knowing about.

    ``skipped`` counts everything this wipe declined to act on rather than
    guessed at, from both halves of it:

    * a row whose payload would not decode, or did not name a scope;
    * a companion row a record claimed but could not prove it owns (see the
      module docstring);
    * from :class:`~wattracker.cloud.storage.ScopePurge`: an object row whose
      stored ``BlobName`` named a blob outside its own partition (that blob
      was not followed, though the row's own blob was still deleted); an
      object row whose key yields no object id, which is **kept** so the
      inconsistency stays visible after its blob is gone; and a name the blob
      listing returned from outside the scope's own prefix.

    None of these is an error and none of them stops the wipe.  A non-zero
    ``skipped`` means a row somewhere disagrees with the row that addresses
    it, which on this table is evidence worth looking at.
    """

    namespace: str
    local_user_scope: str
    records: Mapping[str, int] = field(default_factory=dict)
    objects: int = 0
    blobs: int = 0
    markers: int = 0
    skipped: int = 0
    complete: bool = True
    orphans: int = 0

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


@dataclass(frozen=True)
class _Companion:
    """A companion row key, or a refusal to name one.

    ``key`` is a row key this wipe has *proved* belongs to the record being
    deleted.  ``unproven`` says the record named a companion that could not be
    tied back to it, so nothing was deleted and the operator is told; it is
    never both.
    """

    key: str | None = None
    unproven: bool = False


#: Nothing named, nothing claimed -- the record owns no companion row.
_NO_COMPANION: Final = _Companion()
#: A companion was named and could not be proved; refuse and report.
_UNPROVEN_COMPANION: Final = _Companion(unproven=True)


def _device_seen_companion(row_key: str, value: Mapping[str, Any]) -> _Companion:
    """The ``device-seen`` key a device row owns, proved against its address.

    ``device-seen`` carries no namespace and no scope of its own, so it can
    never be matched on one; it is reachable only through the device row that
    owns it.  That makes the derivation itself the access control, and a
    derivation from an unchecked payload field is no access control at all.

    A device row is *addressed* by ``sha256(credential_id)``, and ``device-
    seen`` by the same digest of the same id.  The threat model includes a
    compromised read plane holding ``entities/write`` on ``authTable``
    (``infra/azure/main.bicep``), which can rewrite any payload; an edited
    ``credential_id`` naming another rider's device made a wipe of this scope
    delete *that* rider's ``device-seen`` row.  So the id is proved against
    the row it sits in -- exactly as ``security._device_id_from_value`` does
    for the device listing, and reusing that function rather than restating
    it.  A payload that names a different credential describes a row this
    record does not own, and is skipped.

    The proof is total, not best-effort: the row key *is* the digest, so a
    payload that passes cannot name anything but this row's own companion.
    """

    credential_id = _device_id_from_value(row_key, value)
    if credential_id is None:
        return _UNPROVEN_COMPANION
    # Re-derived rather than reusing ``row_key`` so the key is canonically
    # spelled, whatever case the stored key happened to use.
    return _Companion(key=_digest_token(credential_id).hex())


def _context_index_companion(
    row_key: str, value: Mapping[str, Any], backend: StateBackend
) -> _Companion:
    """The ``context-index`` key a context row owns, proved through the index.

    A context row is addressed by ``sha256(token)`` while its index is
    addressed by ``sha256(context_id)`` -- two different digests of two
    different secrets, so unlike a device row this one cannot re-derive its
    own address from its payload.  The binding runs the other way instead:
    the index stores ``token_digest``, which is the context row's own key, so
    the index is read back and required to name *this* row before it is
    deleted.  A payload edited to another rider's ``context_id`` points at an
    index naming that rider's context, which fails the comparison and is
    skipped -- previously it broke their reader-context lookups.

    This is not a proof that the pair is untampered, and it is not meant to
    be: an attacker who can rewrite both rows has already broken the victim's
    lookup by rewriting the index, without needing a wipe to do it.  What the
    check buys is that a wipe is not a *lever* -- deleting an index requires
    that index to already point at the row asking for it, so no single edit
    turns one rider's wipe into another rider's deletion.

    A context whose index is missing or does not point back is left with an
    orphan: a row holding a token digest and an expiry, naming no rider,
    resolving to nothing once the context row is gone, and removed by the
    ordinary sweep when it expires.  That is reported in ``skipped``.
    """

    context_id = value.get("context_id")
    if not isinstance(context_id, str) or not context_id:
        return _UNPROVEN_COMPANION
    try:
        candidate = _digest_token(context_id).hex()
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return _UNPROVEN_COMPANION
    index = backend.read("context-index", candidate)
    if not isinstance(index, Mapping):
        return _UNPROVEN_COMPANION
    stored_digest = index.get("token_digest")
    if not isinstance(stored_digest, str):
        return _UNPROVEN_COMPANION
    try:
        if not hmac.compare_digest(stored_digest, row_key):
            return _UNPROVEN_COMPANION
    except TypeError:  # pragma: no cover - non-ASCII in an edited row
        return _UNPROVEN_COMPANION
    return _Companion(key=candidate)


def wipe_capability_proven(
    namespace: str,
    local_user_scope: str,
    *,
    store: object | None,
    security_backend: object | None,
) -> bool:
    """Whether every store a wipe deletes from will let this identity delete.

    Call this before :func:`wipe_scope` and refuse the wipe on ``False``.
    :func:`wipe_scope` removes credentials first and data last, so a delete
    grant that is missing on the *data* stores -- as it is for the minutes
    Azure RBAC takes to propagate a new role assignment -- would otherwise
    delete the rider's credentials and then 403 on the purge, leaving the data
    in the cloud with no credential left to retry with.  Proving capability
    first turns that into a clean refusal with nothing deleted.

    Three stores are proved, and each with a delete of a key that cannot
    exist, never of real data:

    * the object blobs and the object rows, through the store's
      ``can_purge_scope`` (which also takes and releases the scope lease the
      purge holds while it deletes);
    * the credential rows, through the auth backend's ``can_delete``.

    A collaborator that is ``None`` is not asked -- :func:`wipe_scope` will
    not touch it either.  A collaborator that has no probe, or whose probe
    raises, is "cannot": this answers "yes" only on positive evidence.
    """

    namespace_text = _require_namespace(namespace)
    scope_text = _require_local_scope(local_user_scope)
    if store is None and security_backend is None:
        return False
    checks: list[tuple[str, Any]] = []
    if store is not None:
        checks.append((
            "object store",
            lambda: store.can_purge_scope(namespace_text, scope_text),
        ))
    if security_backend is not None:
        checks.append(("auth table", lambda: security_backend.can_delete()))
    for name, check in checks:
        try:
            proven = check() is True
        except Exception:
            proven = False
        if not proven:
            # Which store, never which key: the probe key is random and the
            # scope is the rider's.  This is the line an operator reads to
            # learn "the grant has not reached this store yet".
            _log.warning(
                "cloud scope wipe refused: the %s did not prove delete "
                "permission; nothing was deleted",
                name,
            )
            return False
    return True


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

    This function does not probe permissions itself.  A caller running
    against a real deployment must ask :func:`wipe_capability_proven` first
    and refuse on ``False``, as the HTTP route does: once this has started,
    the credentials are the first thing to go.
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
                # The companion is decided *before* the parent row goes, and
                # the parent goes either way.  A tampered payload does not
                # make the record stop being a credential for this scope:
                # ``_find_device_locked`` and ``read_context_token`` both
                # resolve from the row's own address and never read the field
                # that was edited, so a row left behind here would still
                # authenticate a device, or still authorize reads on a scope
                # the rider asked to have emptied.  Only the *companion*
                # deletion is withheld, because only that one reaches a row
                # this record has not proved it owns.
                if kind == "device":
                    companion = _device_seen_companion(key, value)
                    if companion.key is not None:
                        _delete(DEVICE_SEEN_RECORD_KIND, companion.key)
                elif kind == "context":
                    companion = _context_index_companion(key, value, backend)
                    if companion.key is not None:
                        _delete("context-index", companion.key)
                else:
                    companion = _NO_COMPANION
                if companion.unproven:
                    skipped += 1
                    _log.warning(
                        "cloud scope wipe left a %s companion row alone: the "
                        "parent row does not prove it owns it",
                        kind,
                    )
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
        orphans=purge.orphans,
    )
    _log.info(
        "cloud scope wipe removed %d records and %d objects (complete=%s)",
        report.total_records,
        report.objects,
        report.complete,
    )
    if report.orphans:
        _log.warning(
            "cloud scope wipe deleted %d blob(s) that no table row named: an "
            "earlier sync was interrupted between writing a blob and its row",
            report.orphans,
        )
    return report


__all__ = [
    "ScopeWipeReport",
    "WIPE_PROTECTED_RECORD_KINDS",
    "WIPE_RECORD_KINDS",
    "WIPE_SCAN_LIMIT",
    "wipe_capability_proven",
    "wipe_scope",
]
