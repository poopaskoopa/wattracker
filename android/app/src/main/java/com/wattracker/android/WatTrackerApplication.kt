package com.wattracker.android

import android.app.Application
import android.content.Context
import androidx.room.Room
import com.wattracker.android.cache.RoomSnapshotCache
import com.wattracker.android.cache.WatTrackerDatabase
import com.wattracker.android.cloud.CloudClient
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.DeviceCredentialStore
import com.wattracker.android.cloud.DeviceKeyStore
import com.wattracker.android.cloud.EncryptedDeviceCredentialStore
import com.wattracker.android.cloud.HttpUrlCloudTransport
import com.wattracker.android.cloud.InMemoryDeviceCredentialStore
import com.wattracker.android.cloud.SharedPreferencesRemovalGate
import com.wattracker.android.cloud.LocalClient
import com.wattracker.android.cloud.ReadModel
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * Builds and owns the process-wide cloud session, cache, and read seam.
 *
 * Setup is heavy -- two `EncryptedSharedPreferences` (Keystore + Tink keyset),
 * a Keystore key, and Room -- so it runs lazily off the main thread, never
 * synchronously in [onCreate]. The screens depend on the
 * [com.wattracker.android.cloud.ReadSession] seam, so they get a [ReadModel]
 * from the suspend accessors below and read `isPaired` off it; the
 * pairing/signing/revocation machinery stays here.
 *
 * A store that will not open -- a corrupt Room file, a corrupt encrypted
 * credential prefs -- is reset rather than allowed to brick the app: the
 * result is an unpaired device, not a crash on every launch.
 */
class WatTrackerApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        WatTrackerApp.start(this)
    }
}

object WatTrackerApp {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val lock = Any()
    private var initJob: CompletableDeferred<Unit>? = null

    @Volatile private var database: WatTrackerDatabase? = null
    @Volatile private var session: CloudSession? = null
    @Volatile private var readModel: ReadModel? = null
    @Volatile private var deviceKey: DeviceKeyStore.DeviceKey? = null

    val isReady: Boolean
        get() = readModel != null

    /** Where the signing key lives (StrongBox / TEE / software), for Settings. */
    val keyKind: DeviceKeyStore.KeyKind?
        get() = deviceKey?.kind

    /**
     * Set when the encrypted credential store will not open and the in-memory
     * fallback is serving: a pairing then works for this launch only and is
     * lost on the next start. #195's pairing screen must surface this instead
     * of letting the rider pair into a void; until then this is the record.
     */
    @Volatile
    var credentialStoreDegraded: Boolean = false
        private set

    /** Kick the (blocking) setup off the main thread. Idempotent. */
    fun start(context: Context) {
        ensureStarted(context)
    }

    suspend fun readModel(context: Context): ReadModel {
        ensureStarted(context).await()
        return readModel ?: error("read model unavailable")
    }

    suspend fun session(context: Context): CloudSession {
        ensureStarted(context).await()
        return session ?: error("session unavailable")
    }

    private fun ensureStarted(context: Context): CompletableDeferred<Unit> = synchronized(lock) {
        initJob?.let { return@synchronized it }
        val job = CompletableDeferred<Unit>()
        initJob = job
        scope.launch {
            try {
                doInit(context.applicationContext)
                job.complete(Unit)
            } catch (t: Throwable) {
                // Unrecoverable: allow a later retry and let the caller handle it.
                synchronized(lock) { if (initJob === job) initJob = null }
                job.completeExceptionally(t)
            }
        }
        job
    }

    private fun doInit(context: Context) {
        val db = openDatabase(context)
        val credentials = openCredentialStore(context)
        val key = openSigner()
        val client = CloudClient(
            baseScheme = BuildConfig.WATTRACKER_CLOUD_SCHEME,
            baseAuthority = BuildConfig.WATTRACKER_CLOUD_AUTHORITY,
            signer = key.signer,
            transport = HttpUrlCloudTransport(),
        )
        val session = CloudSession(
            client = client,
            credentials = credentials,
            cache = RoomSnapshotCache(db),
            removalGate = SharedPreferencesRemovalGate(context),
        )
        database = db
        this.session = session
        deviceKey = key
        readModel = ReadModel(session, LocalClient())
    }

    private fun openDatabase(context: Context): WatTrackerDatabase {
        val builder = Room.databaseBuilder(context, WatTrackerDatabase::class.java, WatTrackerDatabase.FILE_NAME)
        val database = builder.build()
        return try {
            // build() is lazy: the file opens on first use, so a corrupt file
            // would throw on the first read -- long after this, and outside
            // any catch. An empty transaction forces the open now, where the
            // reset below can see it.
            database.runInTransaction { }
            database
        } catch (e: Exception) {
            // A corrupt file or schema drift: drop the actual database file
            // (it lives under databases/, not filesDir, so deleteFile would be
            // a no-op) and start with no cache.
            runCatching { database.close() }
            context.deleteDatabase(WatTrackerDatabase.FILE_NAME)
            builder.build()
        }
    }

    private fun openCredentialStore(context: Context): DeviceCredentialStore {
        return try {
            EncryptedDeviceCredentialStore(context)
        } catch (_: Exception) {
            try {
                // A corrupt encrypted-prefs file: drop the actual prefs file
                // (shared_prefs/, not filesDir) and retry.
                context.deleteSharedPreferences(EncryptedDeviceCredentialStore.FILE_NAME)
                EncryptedDeviceCredentialStore(context)
            } catch (_: Exception) {
                // The encrypted store itself will not open (a corrupt Tink
                // master keyset): start unpaired from memory rather than
                // bricking the app. There is no credential to protect that is
                // not also missing.
                //
                // But the fallback is not silent: with it in place a pairing
                // "succeeds" for this launch and is lost on the next start,
                // so the state is recorded -- #195's pairing screen must show
                // it instead of letting the rider pair into a void.
                credentialStoreDegraded = true
                InMemoryDeviceCredentialStore()
            }
        }
    }

    private fun openSigner(): DeviceKeyStore.DeviceKey {
        // A missing key is generated on first run. If the Keystore itself is
        // unusable this throws and init fails, surfaced to the caller rather
        // than crashing -- a cloud session cannot sign without a key, so
        // there is no graceful unpaired fallback for this one.
        return DeviceKeyStore().loadOrCreate()
    }
}
