package com.wattracker.android.cloud

/**
 * One route's last-known objects, and the checkpoint they were read at.
 *
 * The revision is the server's, copied back verbatim and never computed here.
 * It is the only thing that makes the delta correct: `since=` is answered
 * against the scope's own counter, and a client that invented a checkpoint
 * would ask for a window the server never promised and silently miss whatever
 * fell outside it.
 */
data class CachedCollection(
    val revision: Int,
    val items: List<CloudItem>,
    /** Epoch millis, for how old a `.cache` snapshot is. */
    val storedAt: Long,
)

/** Where last-known data lives between launches. */
interface SnapshotCache {
    fun load(route: CloudRoute): CachedCollection?

    /**
     * Persist the objects a completed read returned, and advance the route's
     * checkpoint to [revision].
     *
     * [received] is what the server sent for this read -- the whole set for a
     * [full] read, or only the changed objects (including tombstones) for a
     * delta -- not an already-merged collection. The cache applies it, so a
     * delta touches only the rows that changed instead of rewriting the route.
     *
     * [generation] is the lifecycle identity the read ran under. A write is
     * refused once a newer identity has committed (a removal or re-pairing),
     * so a read still in flight cannot put the previous device's objects back
     * on disk after the wipe. The identity is process-local (a [GenerationGate]
     * in memory, never on disk): a restarted process is a fresh identity, and
     * its writes must land against the rows the prior launch stored.
     */
    fun store(received: List<CloudItem>, revision: Int, route: CloudRoute, full: Boolean, generation: Long)

    /** Forget one route. A corrupt row is dropped this way rather than fatal. */
    fun dropRoute(route: CloudRoute)

    /**
     * Forget everything. Called on revocation, where it is the point.
     * [generation] is the identity that wiped, so a stale write from an older
     * identity is refused afterwards.
     */
    fun removeAll(generation: Long)
}

/**
 * The identity gate behind the generation-stamped cache writes.
 *
 * [accepts] refuses a write from an identity older than one already
 * committed, so a read that is still in flight cannot put a wiped device's
 * objects back after a removal or re-pairing. [commit] records the identity
 * a write (or wipe) ran under.
 *
 * Process-local on purpose: an in-flight read cannot outlive the process that
 * started it (the iOS session's `lifecycleGeneration` is the same shape), so
 * the gate must not outlive it either. Persisting the highest committed
 * identity lets a dead process veto a living one -- after a restart the
 * fresh session's identity sits below the persisted epoch and every write is
 * refused, freezing the cache at the prior launch's revision. The gate lives
 * in memory and is born with the process.
 */
class GenerationGate {
    @Volatile private var committed = 0L

    /** True if a write under [generation] may land. */
    fun accepts(generation: Long): Boolean = generation >= committed

    /** Record that [generation] committed; older identities are refused after. */
    fun commit(generation: Long) {
        if (generation > committed) committed = generation
    }
}

/**
 * The in-process cache: the tests use it, and it is the reference for the
 * [merge] rule. It also lets a build with no disk access fall back to
 * last-known data for the life of the process.
 *
 * [backing] exists so a test can share one map between two cache instances --
 * the rows survive a "restart" while each instance keeps its own in-memory
 * gate, the exact shape of the Room cache.
 */
class InMemorySnapshotCache(
    private val clock: () -> Long = { System.currentTimeMillis() },
    private val backing: MutableMap<CloudRoute, CachedCollection> = HashMap(),
) : SnapshotCache {

    private val lock = Any()
    private val gate = GenerationGate()

    override fun load(route: CloudRoute): CachedCollection? = synchronized(lock) { backing[route] }

    override fun store(received: List<CloudItem>, revision: Int, route: CloudRoute, full: Boolean, generation: Long) {
        synchronized(lock) {
            if (!gate.accepts(generation)) return
            val items = if (full) {
                received.filter { !it.deleted }.sortedBy { it.id }
            } else {
                merge(received, backing[route])
            }
            backing[route] = CachedCollection(revision, items, clock())
            gate.commit(generation)
        }
    }

    override fun dropRoute(route: CloudRoute) {
        synchronized(lock) { backing.remove(route) }
    }

    override fun removeAll(generation: Long) {
        synchronized(lock) {
            backing.clear()
            gate.commit(generation)
        }
    }
}

/**
 * Apply what arrived to what was cached.
 *
 * `cached == null` is a full read and replaces outright. Otherwise this is a
 * delta: an object carrying `deleted` is a tombstone and removes its key,
 * everything else is an upsert. The result is ordered by object id, the order
 * the server pages in, so a merged collection reads the same way a freshly
 * fetched one does.
 *
 * The feed is at-least-once, so a replay can deliver an object at an *older*
 * revision than one already cached; upserting blindly would clobber the newer
 * copy. An incoming object only wins at its own revision -- a stale replay is
 * dropped, and a stale tombstone cannot remove an object a later read
 * recreated. Objects are keyed by (kind, id): the same id can appear under two
 * kinds, and the plan and the Room schema both key on kind, not id alone.
 */
fun merge(received: List<CloudItem>, cached: CachedCollection?): List<CloudItem> {
    val byKey = LinkedHashMap<Pair<CloudKind, String>, CloudItem>()
    for (item in cached?.items ?: emptyList()) byKey[item.kind to item.id] = item
    for (item in received) {
        val key = item.kind to item.id
        val existing = byKey[key]
        if (existing != null && item.revision < existing.revision) continue
        if (item.deleted) byKey.remove(key) else byKey[key] = item
    }
    return byKey.values.sortedBy { it.id }
}
