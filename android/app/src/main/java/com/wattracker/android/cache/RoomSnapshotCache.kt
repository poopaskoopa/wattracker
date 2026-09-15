package com.wattracker.android.cache

import com.wattracker.android.cloud.CachedCollection
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.GenerationGate
import com.wattracker.android.cloud.SnapshotCache
import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.toJson

/**
 * The [SnapshotCache] over Room.
 *
 * A route's rows are applied, not rewritten: a full read replaces the route,
 * but a delta upserts only the objects the server sent (gated on revision so
 * an at-least-once replay cannot clobber a newer row) and deletes its
 * tombstones at the same gate, then sets the route's checkpoint to the
 * revision the server returned. A write from an identity older than one
 * already committed is refused (the [GenerationGate]), so a read still in
 * flight cannot put a wiped device's objects back. The gate is in memory, not
 * in the `meta` table: it guards against in-flight writes of *this* process,
 * and an in-flight read cannot outlive the process -- a persisted epoch let a
 * dead process veto the fresh identity a restart starts at, and every write
 * after the first relaunch was refused. All of the disk work is in one
 * transaction, which is also what serializes the gate's access (the
 * transaction body runs on the database thread). The checkpoint's `fetched_at`
 * is the age the UI reports, so an empty collection reads as
 * recently-fetched rather than brand-new.
 */
class RoomSnapshotCache(
    private val database: WatTrackerDatabase,
    private val clock: () -> Long = { System.currentTimeMillis() },
) : SnapshotCache {

    private val objectsDao = database.cloudObjectsDao()
    private val checkpointDao = database.checkpointDao()

    // Touched only inside `runInTransaction` blocks, which Room serializes on
    // the database thread, so no lock of its own.
    private val gate = GenerationGate()

    override fun load(route: CloudRoute): CachedCollection? {
        val checkpoint = checkpointDao.get(route.path) ?: return null
        val rows = objectsDao.load(route.path)
        return try {
            CachedCollection(
                revision = checkpoint.revision.toInt(),
                items = rows.map { CloudItem.fromJson(JsonValue.parse(it.dataJson)) },
                storedAt = checkpoint.fetchedAt,
            )
        } catch (_: Exception) {
            // A row that will not decode is dropped, not fatal: the route is
            // empty for now and the next read refetches it from the server.
            dropRoute(route)
            null
        }
    }

    override fun store(received: List<CloudItem>, revision: Int, route: CloudRoute, full: Boolean, generation: Long) {
        val now = clock()
        database.runInTransaction {
            // A write from an identity older than one already committed is a
            // stale replay: refuse it so a read in flight cannot put a wiped
            // device's objects back after a removal or re-pairing.
            if (!gate.accepts(generation)) return@runInTransaction
            val path = route.path
            if (full) {
                objectsDao.deleteRoute(path)
                val live = received.filter { !it.deleted }
                if (live.isNotEmpty()) objectsDao.upsertAll(live.map { entity(path, it, now) })
            } else {
                for (item in received) {
                    val kind = item.kind.wire
                    val stored = objectsDao.revisionOf(path, kind, item.id)
                    // A tombstone deletes only at a revision it can beat, the
                    // same rule merge applies in memory: a stale replayed
                    // tombstone must not erase a row a newer read restored.
                    if (item.deleted) {
                        if (stored == null || item.revision >= stored) objectsDao.deleteByKey(path, kind, item.id)
                    } else if (stored == null || item.revision >= stored) {
                        objectsDao.upsert(entity(path, item, now))
                    }
                }
            }
            checkpointDao.upsert(CheckpointEntity(path, revision.toLong(), now))
            gate.commit(generation)
        }
    }

    override fun dropRoute(route: CloudRoute) {
        database.runInTransaction {
            val path = route.path
            objectsDao.deleteRoute(path)
            checkpointDao.deleteRoute(path)
        }
    }

    override fun removeAll(generation: Long) {
        // Only the cloud objects and their checkpoints. The local data source's
        // payloads and the app settings are a different backend's state and
        // survive a cloud removal. Recording the wiping identity in the gate
        // means a read from a superseded identity cannot write back after the
        // wipe.
        database.runInTransaction {
            objectsDao.deleteAll()
            checkpointDao.deleteAll()
            gate.commit(generation)
        }
    }

    private fun entity(route: String, item: CloudItem, fetchedAt: Long) = CloudObjectEntity(
        route = route,
        kind = item.kind.wire,
        id = item.id,
        revision = item.revision.toLong(),
        dataJson = item.toJson().toJson(),
        fetchedAt = fetchedAt,
    )
}
