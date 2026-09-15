package com.wattracker.android.cloud

import kotlin.math.max

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
     * on disk after the wipe.
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
 * The in-process cache: the tests use it, and it is the reference for the
 * [merge] rule. It also lets a build with no disk access fall back to
 * last-known data for the life of the process.
 */
class InMemorySnapshotCache(
    private val clock: () -> Long = { System.currentTimeMillis() },
) : SnapshotCache {

    private val lock = Any()
    private val store = HashMap<CloudRoute, CachedCollection>()
    private var epoch = 0L

    override fun load(route: CloudRoute): CachedCollection? = synchronized(lock) { store[route] }

    override fun store(received: List<CloudItem>, revision: Int, route: CloudRoute, full: Boolean, generation: Long) {
        synchronized(lock) {
            if (generation < epoch) return
            val items = if (full) received.sortedBy { it.id } else merge(received, store[route])
            store[route] = CachedCollection(revision, items, clock())
            epoch = max(epoch, generation)
        }
    }

    override fun dropRoute(route: CloudRoute) {
        synchronized(lock) { store.remove(route) }
    }

    override fun removeAll(generation: Long) {
        synchronized(lock) {
            store.clear()
            epoch = max(epoch, generation)
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
