package com.wattracker.android.cloud

/**
 * What the rider asked for, which is not what the app reads.
 *
 * [resolveBackend] turns a selection into the [DataSource] a screen is served
 * from, and only an explicit choice is ever stored. iOS is the reference
 * (`Shell/SessionGate.swift:65-79`): its `Selection` is this enum, and its
 * `manualOverride` is the stored `Cloud` or `Local` half of it.
 *
 * The pairing screen's temporary pick (A1) is deliberately *not* a member here.
 * It is session-local -- iOS's `temporaryOverride` (`:206`, `:541-548`) -- and
 * it changes the resolved backend without a selection and without a write, so
 * pairing on one backend while Automatic is chosen never leaves a preference
 * behind that outlives the screen.
 */
enum class BackendSelection {
    Automatic,
    Cloud,
    Local,
    ;

    /** True when the rider pinned a backend. It is the only thing persisted. */
    val isOverride: Boolean
        get() = this != Automatic

    companion object {
        /**
         * The explicit choice that means "this backend, and only this one".
         *
         * A control that offers only backends cannot name `Automatic`, so the
         * mapping goes one way and lives here rather than in every screen.
         */
        fun of(source: DataSource): BackendSelection = when (source) {
            DataSource.Cloud -> Cloud
            DataSource.Local -> Local
        }
    }
}

/**
 * The backend a launch starts on, with no probe.
 *
 * Automatic prefers the cloud, then a paired desktop, then the cloud again so a
 * first run lands in the cloud pairing flow (`SessionGate.swift:316`). Whether
 * a paired desktop is *reachable* is a different question, and answering it is
 * the probe's job (A2): the desktop is the only backend that can be paired and
 * offline at once, and the 3 s bound is what stops that from holding up a
 * launch.
 */
fun resolveBackend(selection: BackendSelection, cloudPaired: Boolean, localPaired: Boolean): DataSource = when (selection) {
    BackendSelection.Cloud -> DataSource.Cloud
    BackendSelection.Local -> DataSource.Local
    BackendSelection.Automatic -> when {
        cloudPaired -> DataSource.Cloud
        localPaired -> DataSource.Local
        else -> DataSource.Cloud
    }
}

/**
 * What one launch actually learned about one backend's pairing.
 *
 * The three states exist because "not paired" and "could not look" pull in
 * opposite directions. A backend this launch could not inspect is not known to
 * be unpaired, and treating it as such lets one bad launch delete a choice the
 * rider made on a good one.
 */
enum class BackendObservation {
    /** No look was possible: no signing key, so no cloud session; a Keystore that would not open the vault, so no local read. */
    Unknown,

    /** Looked, and found nothing usable: no pairing, or a vault file this installation can never open again. */
    Unpaired,

    Paired,
    ;

    /** Only a seen [Unpaired] is evidence against an override. */
    val contradictsOverride: Boolean
        get() = this == Unpaired
}

/**
 * What this launch may claim about the desktop.
 *
 * [StoreHealth.Unavailable] is the Keystore refusing the vault outright: the
 * in-memory store that stands in never opens the file on disk, so a pairing may
 * be sitting in it unread and nothing about it has been seen.
 * [StoreHealth.UnreadableVault] is the opposite -- the file was opened, and the
 * key behind it is gone -- so the pairing behind it is genuinely gone, and that
 * is a look that says unpaired.
 */
fun observeLocalBackend(vaultHealth: StoreHealth, paired: Boolean): BackendObservation = when {
    vaultHealth == StoreHealth.Unavailable -> BackendObservation.Unknown
    paired -> BackendObservation.Paired
    else -> BackendObservation.Unpaired
}

/**
 * What this launch may claim about the cloud. No signing key means no session,
 * and no session means nobody looked: [BackendObservation.Unknown].
 */
fun observeCloudBackend(sessionBuilt: Boolean, paired: Boolean): BackendObservation = when {
    !sessionBuilt -> BackendObservation.Unknown
    paired -> BackendObservation.Paired
    else -> BackendObservation.Unpaired
}

/**
 * Whether a stored override may survive startup.
 *
 * An override naming a backend that was *seen* unpaired is dropped, and its
 * preference goes with it (`SessionGate.swift:305-312`): left standing it would
 * pin the app to a pairing screen while the other backend worked. `Automatic`
 * is never dropped -- it names no backend in particular -- and an override
 * naming a backend this launch could not inspect is kept, because nothing ever
 * proved it wrong. A dead Keystore is a bad launch, not a rider's change of
 * mind.
 *
 * The local half follows the same rule, state by state. [BackendObservation.Unknown]
 * when the Keystore refused the vault outright: the in-memory store never opens
 * the file on disk, so a pairing may be sitting in it unread. [BackendObservation.Unpaired]
 * when the vault file was opened and this installation cannot use it -- the key
 * behind it is gone, the pairing with it, and Settings already says so. That one
 * is dropped.
 *
 * This is a startup rule only. At runtime an override stands even when the
 * backend it names is unpaired -- that is the case A2's Cancel exists for, and
 * iOS reaches `.unpaired` with `pairedFallback` set rather than silently
 * switching back (`:434`, `:455-463`).
 */
fun keepOverride(
    selection: BackendSelection,
    cloud: BackendObservation,
    local: BackendObservation,
): Boolean = when (selection) {
    BackendSelection.Automatic -> true
    BackendSelection.Cloud -> !cloud.contradictsOverride
    BackendSelection.Local -> !local.contradictsOverride
}
