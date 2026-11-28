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
 * [StoreHealth.Unread] is the same answer for a different reason: the file is
 * there, but this launch could not read it (a transient I/O or provider failure),
 * so again nothing was seen. Neither is evidence against a stored choice.
 * A member this build cannot parse is the same answer with a per-member twist:
 * the file was read and the envelope opened, and the pairing is sitting there --
 * only this build cannot read it, so the launch concluded nothing about that
 * backend. The flag is per member, so a cloud half this build cannot parse does
 * not leave the desktop half unconcluded either.
 * [StoreHealth.UnreadableVault] is the opposite -- the file was opened, and the
 * key behind it is gone -- so the pairing behind it is genuinely gone, and that
 * is a look that says unpaired.
 */
fun observeLocalBackend(
    vaultHealth: StoreHealth,
    paired: Boolean,
    memberUnparsed: Boolean = false,
): BackendObservation = when {
    vaultHealth == StoreHealth.Unavailable -> BackendObservation.Unknown
    // Paired before Unread: a vault with one unparsed member still serves the
    // member that parsed, so a desktop paired from it really is paired.
    paired -> BackendObservation.Paired
    (vaultHealth == StoreHealth.Unread) || memberUnparsed -> BackendObservation.Unknown
    else -> BackendObservation.Unpaired
}

/**
 * What this launch may claim about the cloud.
 *
 * No signing key means no session, and no session means nobody looked:
 * [BackendObservation.Unknown].
 *
 * A built session can still have concluded nothing. The session reads the vault
 * in its constructor, so a Keystore that would not open it ([StoreHealth.Unavailable])
 * or a read that failed ([StoreHealth.Unread]) leaves `isPaired` false without
 * anything having been seen. The cloud binding lives in that same vault, so the
 * vault's health is the cloud's health too -- without this, a Keystore-refused
 * vault would read as a seen-unpaired cloud and delete an explicit `Cloud`
 * override, which is the mistake [observeLocalBackend] exists to avoid.
 * A member this build cannot parse is the same shape: the envelope opened, the
 * pairing is on disk, this build cannot read it.
 *
 * [StoreHealth.UnreadableVault] is deliberately not `Unknown`: the envelope
 * opened and this installation cannot use it, so the pairing behind it is gone.
 * That is a look, and it says unpaired.
 */
fun observeCloudBackend(
    sessionBuilt: Boolean,
    paired: Boolean,
    vaultHealth: StoreHealth,
    memberUnparsed: Boolean = false,
): BackendObservation = when {
    !sessionBuilt -> BackendObservation.Unknown
    vaultHealth == StoreHealth.Unavailable -> BackendObservation.Unknown
    paired -> BackendObservation.Paired
    (vaultHealth == StoreHealth.Unread) || memberUnparsed -> BackendObservation.Unknown
    else -> BackendObservation.Unpaired
}

/**
 * What one launch concludes about the two backends, in the right order.
 *
 * A failure is read in one direction: the pairing flags are read first and
 * the vault's health is read after them. `local.isPaired` triggers the vault's
 * first read -- the cloud session's constructor does the same on the common
 * path -- and health answers from that read, so reading health first would
 * report `Durable` for a vault whose read has not happened yet: a Keystore
 * outage that leaves both the signer and the cipher broken would then read
 * the desktop as a seen-unpaired backend and delete a stored `Local` choice.
 *
 * The seam takes plain booleans so the ordering is pinned by a test instead
 * of by the caller's argument list, where Kotlin's left-to-right evaluation
 * is easy to get wrong.
 */
data class LaunchObservations(
    val cloud: BackendObservation,
    val local: BackendObservation,
)

fun launchObservations(
    cloudSessionBuilt: Boolean,
    cloudPaired: () -> Boolean,
    localPaired: () -> Boolean,
    vaultHealth: () -> StoreHealth,
    cloudMemberUnparsed: () -> Boolean,
    localMemberUnparsed: () -> Boolean,
): LaunchObservations {
    // Read the pairing flags first, then health. The paired closures may read
    // the vault; health must answer from that read, not from before it.
    val cloudPairedValue = cloudPaired()
    val localPairedValue = localPaired()
    val health = vaultHealth()
    return LaunchObservations(
        cloud = observeCloudBackend(
            sessionBuilt = cloudSessionBuilt,
            paired = cloudPairedValue,
            vaultHealth = health,
            memberUnparsed = cloudMemberUnparsed(),
        ),
        local = observeLocalBackend(
            vaultHealth = health,
            paired = localPairedValue,
            memberUnparsed = localMemberUnparsed(),
        ),
    )
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

/**
 * The whole startup decision: what the launch starts on, and whether the stored
 * preference has to be deleted.
 *
 * [keepOverride] alone cannot be pinned from a screen, and the wiring around it
 * -- dropping a kept override anyway, or forgetting to delete one that was
 * dropped -- would stay green. So the two outcomes are decided here, together,
 * and `doInit` only applies them. [deleteStoredPreference] is true exactly when
 * something is stored and is not being kept, which is also the rule for a value
 * this build does not recognise: it can only have been edited by hand, and
 * leaving it would mean re-reading the same garbage every launch.
 */
data class StartupSelection(
    val selection: BackendSelection,
    val deleteStoredPreference: Boolean,
)

fun decideStartupSelection(
    storedName: String?,
    stored: BackendSelection?,
    cloud: BackendObservation,
    local: BackendObservation,
): StartupSelection {
    val kept = stored != null && keepOverride(stored, cloud, local)
    return StartupSelection(
        selection = if (kept) stored else BackendSelection.Automatic,
        // `storedName` is the raw preference, so an unrecognised value -- which
        // parses to a null `stored` -- is deleted rather than re-read forever.
        deleteStoredPreference = storedName != null && !kept,
    )
}

/**
 * The rider's choice and the generation it was made under, one guard.
 *
 * The review's finding B was a window, not a lock: a resolve that reads the
 * generation first and the selection after can capture the pre-change
 * selection under the post-change generation, pass the check, and write the
 * backend the rider just replaced. Both reads happen together, under the lock
 * that also bumps the counter on a set, so no read can straddle a write.
 *
 * This is the pure half: [snapshot] is the read a resolve starts from,
 * [dropIfStale] is the guarded write. The Application owns the prefs side
 * and the flows; this owns only the invariant the tests pin.
 */
class SelectionGuard {
    private val lock = Any()
    private var generation = 0
    private var current: BackendSelection = BackendSelection.Automatic

    /** The generation and the selection, read together, never torn. */
    fun snapshot(): Pair<Int, BackendSelection> = synchronized(lock) { generation to current }

    /** Record the rider's choice; everything snapshotted before it is stale. */
    fun set(selection: BackendSelection) = synchronized(lock) {
        generation += 1
        current = selection
    }

    /**
     * The guarded write: a resolve drops its result unless the generation it
     * snapshotted is still the one this guard holds.
     */
    fun dropIfStale(snapshotted: Pair<Int, BackendSelection>, resolve: (BackendSelection) -> Unit): Boolean {
        synchronized(lock) {
            if (generation != snapshotted.first) return false
        }
        resolve(synchronized(lock) { current })
        return true
    }
}
