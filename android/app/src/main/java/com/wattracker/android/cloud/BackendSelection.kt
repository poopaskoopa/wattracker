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
 * Whether a stored override may survive startup.
 *
 * An override naming a backend that is not paired would pin the app to a
 * pairing screen while the other backend was working, so it is dropped and its
 * preference goes with it (`SessionGate.swift:305-312`). `Automatic` is never
 * dropped: it names no backend in particular.
 *
 * This is a startup rule only. At runtime an override stands even when the
 * backend it names is unpaired -- that is the case A2's Cancel exists for, and
 * iOS reaches `.unpaired` with `pairedFallback` set rather than silently
 * switching back (`:434`, `:455-463`).
 */
fun keepOverride(selection: BackendSelection, cloudPaired: Boolean, localPaired: Boolean): Boolean = when (selection) {
    BackendSelection.Automatic -> true
    BackendSelection.Cloud -> cloudPaired
    BackendSelection.Local -> localPaired
}
