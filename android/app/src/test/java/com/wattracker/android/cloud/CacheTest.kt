package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The [merge] rule the delta read depends on: an upsert wins at its own
 * revision, a tombstone removes its key, and a stale replay (an object at an
 * *older* revision than the cached copy) is dropped so an at-least-once feed
 * cannot clobber a newer row.
 */
class CacheTest {

    @Test
    fun aFullReadWithNothingCachedReplacesOutright() {
        // `cached == null` is a full read: the result is exactly what arrived.
        val merged = merge(listOf(item("x", 1)), null)
        assertEquals(listOf("x"), merged.map { it.id })
    }

    @Test
    fun aDeltaUpsertsInsertsTombstonesAndDropsStaleReplays() {
        val cached = CachedCollection(
            revision = 10,
            items = listOf(item("a", 5), item("b", 7), item("c", 9)),
            storedAt = 0,
        )
        val delta = listOf(
            item("a", 6), // newer than the cached 5: the upsert applies
            item("b", 3), // stale replay (3 < 7): dropped, the base copy survives
            item("c", 9, deleted = true), // a tombstone at the current revision removes it
            item("d", 11), // a plain insert
        )
        val merged = merge(delta, cached)
        val byId = merged.associateBy { it.id }
        assertEquals(6, byId["a"]!!.revision)
        assertEquals(7, byId["b"]!!.revision)
        assertNull(byId["c"])
        assertEquals(11, byId["d"]!!.revision)
        // Ordered by object id, the order the server pages in.
        assertEquals(listOf("a", "b", "d"), merged.map { it.id })
    }

    @Test
    fun aStaleTombstoneCannotRemoveAnObjectALaterReadRecreated() {
        // The cache holds a freshly recreated "a" at revision 5; a replayed
        // tombstone from revision 3 must not erase it.
        val cached = CachedCollection(5, listOf(item("a", 5)), 0)
        val merged = merge(listOf(item("a", 3, deleted = true)), cached)
        assertEquals(listOf("a"), merged.map { it.id })
    }

    private fun item(id: String, revision: Int, deleted: Boolean = false): CloudItem =
        CloudItem.fromJson(
            JsonValue.parse("""{"id":"$id","kind":"profile","revision":$revision,"data":{"ftp":250},"deleted":$deleted}"""),
        )
}
