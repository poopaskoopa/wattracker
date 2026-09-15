package com.wattracker.android.cache

import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.RoomDatabase

/**
 * One stored cloud object: a row per (route, kind, id).
 *
 * [kind] is the object's wire kind beside [id] because the plan keys objects
 * on (kind, id) and the same id could in principle appear under two kinds. The
 * columns serve the primary key and the checkpoint query; [dataJson] is the
 * object's full JSON so a row decodes back to the exact item. There is no
 * `deleted` column: a tombstone removes its row, it is not stored.
 */
@Entity(tableName = "objects", primaryKeys = ["route", "kind", "id"])
data class CloudObjectEntity(
    @ColumnInfo(name = "route") val route: String,
    @ColumnInfo(name = "kind") val kind: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "revision") val revision: Long,
    @ColumnInfo(name = "data_json") val dataJson: String,
    @ColumnInfo(name = "fetched_at") val fetchedAt: Long,
)

/**
 * The per-route checkpoint: the revision the next delta is asked from, and
 * when it was last fetched. The `fetched_at` lives here (not on the objects)
 * so an empty collection still reports the age of its last real read.
 */
@Entity(tableName = "checkpoints", primaryKeys = ["route"])
data class CheckpointEntity(
    @ColumnInfo(name = "route") val route: String,
    @ColumnInfo(name = "revision") val revision: Long,
    @ColumnInfo(name = "fetched_at") val fetchedAt: Long,
)

/** The local data source's published payloads (the `LocalClient` seam). */
@Entity(tableName = "local_payloads", primaryKeys = ["payload_key"])
data class LocalPayloadEntity(
    @ColumnInfo(name = "payload_key") val payloadKey: String,
    @ColumnInfo(name = "payload_json") val payloadJson: String,
    @ColumnInfo(name = "fetched_at") val fetchedAt: Long,
)

/** Small app settings: the data-source toggle and the like. */
@Entity(tableName = "meta", primaryKeys = ["meta_key"])
data class MetaEntity(
    @ColumnInfo(name = "meta_key") val metaKey: String,
    @ColumnInfo(name = "meta_value") val metaValue: String,
)

@Dao
interface CloudObjectsDao {
    @Query("SELECT * FROM objects WHERE route = :route")
    fun load(route: String): List<CloudObjectEntity>

    /** Replace one row; the caller gates on revision first (a delta). */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsert(entity: CloudObjectEntity)

    /** Replace-all for a full read. */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertAll(objects: List<CloudObjectEntity>)

    /** The stored revision for one key, or null if absent. Gates a delta upsert. */
    @Query("SELECT revision FROM objects WHERE route = :route AND kind = :kind AND id = :id")
    fun revisionOf(route: String, kind: String, id: String): Long?

    @Query("DELETE FROM objects WHERE route = :route AND kind = :kind AND id = :id")
    fun deleteByKey(route: String, kind: String, id: String)

    @Query("DELETE FROM objects WHERE route = :route")
    fun deleteRoute(route: String)

    @Query("DELETE FROM objects")
    fun deleteAll()
}

@Dao
interface CheckpointDao {
    @Query("SELECT * FROM checkpoints WHERE route = :route")
    fun get(route: String): CheckpointEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsert(entity: CheckpointEntity)

    @Query("DELETE FROM checkpoints WHERE route = :route")
    fun deleteRoute(route: String)

    @Query("DELETE FROM checkpoints")
    fun deleteAll()
}

@Dao
interface LocalPayloadDao {
    @Query("SELECT * FROM local_payloads WHERE payload_key = :key")
    fun get(key: String): LocalPayloadEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun put(entity: LocalPayloadEntity)

    @Query("DELETE FROM local_payloads")
    fun deleteAll()
}

@Dao
interface MetaDao {
    @Query("SELECT meta_value FROM meta WHERE meta_key = :key")
    fun get(key: String): String?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun put(entity: MetaEntity)

    @Query("DELETE FROM meta")
    fun deleteAll()
}

@Database(
    entities = [CloudObjectEntity::class, CheckpointEntity::class, LocalPayloadEntity::class, MetaEntity::class],
    version = 1,
    // v1 is the baseline for the first migration, so export the schema.
    exportSchema = true,
)
abstract class WatTrackerDatabase : RoomDatabase() {
    abstract fun cloudObjectsDao(): CloudObjectsDao
    abstract fun checkpointDao(): CheckpointDao
    abstract fun localPayloadDao(): LocalPayloadDao
    abstract fun metaDao(): MetaDao

    companion object {
        const val FILE_NAME = "wattracker.db"
    }
}
