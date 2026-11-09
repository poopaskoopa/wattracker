package com.wattracker.android

import android.app.Application
import android.content.Context
import androidx.room.Room
import com.wattracker.android.cache.RoomSnapshotCache
import com.wattracker.android.cache.WatTrackerDatabase
import com.wattracker.android.cloud.BackendSelection
import com.wattracker.android.cloud.CloudApiResult
import com.wattracker.android.cloud.CloudClient
import com.wattracker.android.cloud.CloudDevice
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.DataSource
import com.wattracker.android.cloud.DeviceCredentialStore
import com.wattracker.android.cloud.DeviceKeyStore
import com.wattracker.android.cloud.HttpUrlCloudTransport
import com.wattracker.android.cloud.InMemoryDeviceCredentialStore
import com.wattracker.android.cloud.KeystoreDeviceCredentialStore
import com.wattracker.android.cloud.LocalClient
import com.wattracker.android.cloud.LocalCredentials
import com.wattracker.android.cloud.ReadModel
import com.wattracker.android.cloud.RemoveDeviceResult
import com.wattracker.android.cloud.SharedPreferencesRemovalGate
import com.wattracker.android.cloud.keepOverride
import com.wattracker.android.cloud.resolveBackend
import com.wattracker.android.cloud.StoreHealth
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Builds and owns the process-wide cloud session, local client, cache, and read seam.
 *
 * Setup is heavy -- a Keystore-wrapped credential vault, a Keystore signing
 * key, and Room -- so it runs lazily off the main thread, never synchronously
 * in [onCreate]. The screens depend on the
 * [com.wattracker.android.cloud.ReadSession] seam, so they get a [ReadModel]
 * from the suspend accessors below and read `isPaired` off it; the
 * pairing/signing/revocation machinery stays here.
 *
 * Nothing here is allowed to brick the app: a corrupt Room file is reset to an
 * empty cache, and a credential vault this installation cannot open leaves the
 * device unpaired and says so through [credentialStoreHealth] rather than
 * throwing on every launch.
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
    @Volatile private var client: CloudClient? = null
    @Volatile private var localClient: LocalClient? = null
    @Volatile private var readModel: ReadModel? = null
    @Volatile private var deviceKey: DeviceKeyStore.DeviceKey? = null
    @Volatile private var credentialStore: DeviceCredentialStore? = null
    @Volatile private var cloudFailure: Throwable? = null

    private val _selectionFlow = MutableStateFlow(BackendSelection.Automatic)

    /** What the rider asked for. It is not what the screens read. */
    val selectionFlow: StateFlow<BackendSelection> = _selectionFlow.asStateFlow()

    private val _dataSourceFlow = MutableStateFlow(DataSource.Cloud)

    /** The backend the screens are reading, resolved from [selectionFlow]. */
    val dataSourceFlow: StateFlow<DataSource> = _dataSourceFlow.asStateFlow()

    val isReady: Boolean
        get() = readModel != null

    /**
     * The paired credential id, or null. A read that fails (transient I/O, a
     * Keystore hiccup) answers null for this call rather than throwing into a
     * screen; the reason is carried by [credentialStoreHealth], and no mutation
     * acts on the failed read (#459 M1).
     */
    val currentCredentialId: String?
        get() = try {
            credentialStore?.load()?.credentialId
        } catch (_: Exception) {
            null
        }

    /** Where the signing key lives (StrongBox / TEE / software), for Settings. */
    val keyKind: DeviceKeyStore.KeyKind?
        get() = deviceKey?.kind

    /**
     * Why this launch has no cloud session, or null while it has one.
     *
     * A build failure is not a read result, so it is never thrown into a
     * screen: [ReadModel] answers the cloud as unpaired and this is where the
     * reason waits, so the app-level gate (A1) can tell "not paired yet" from
     * "this device cannot be paired". It is also what B2's release refusal
     * will report, and it must stay reportable without taking the local
     * desktop backend down with it.
     */
    val cloudStartFailure: Throwable?
        get() = cloudFailure

    /**
     * What the credential store can promise about the next launch:
     * [com.wattracker.android.cloud.StoreHealth.Durable] normally,
     * [com.wattracker.android.cloud.StoreHealth.Unavailable] when the Keystore
     * handed over no key (or handed over one whose bytes can be read, which this
     * app refuses) and the in-memory store is serving, and
     * [com.wattracker.android.cloud.StoreHealth.UnreadableVault] when a vault file
     * exists that this installation cannot open -- the key behind it is gone, or
     * the file was carried in from another device, which is what the backup rules
     * exist to prevent.
     *
     * Pairing into the first two "succeeds" for this launch and is not a pairing
     * the rider can rely on, so #195's pairing screen must say which of the two
     * happened instead of letting a rider pair into a void; until that screen
     * exists this is the record, shown on Settings. Read after [doInit], which
     * constructs the session and so reads the vault once before any screen asks.
     */
    val credentialStoreHealth: StoreHealth
        get() = credentialStore?.health ?: StoreHealth.Durable

    /** Kick the (blocking) setup off the main thread. Idempotent. */
    fun start(context: Context) {
        ensureStarted(context)
    }

    suspend fun readModel(context: Context): ReadModel {
        ensureStarted(context).await()
        return readModel ?: error("read model unavailable")
    }

    /** The cloud session, or null when this launch could not build one. */
    suspend fun cloudSessionOrNull(context: Context): CloudSession? {
        ensureStarted(context).await()
        return session
    }

    /** The cloud session, for the flows that cannot work without one. */
    suspend fun session(context: Context): CloudSession {
        ensureStarted(context).await()
        return session ?: throw CloudSession.Failure.NotPaired()
    }

    suspend fun localClient(context: Context): LocalClient {
        ensureStarted(context).await()
        return localClient ?: error("local client unavailable")
    }

    /**
     * Record the rider's choice and re-resolve what that means to read.
     *
     * `Automatic` stores nothing: clearing the preference and selecting it are
     * the same act, so there is no stored `Automatic` to tell apart from a
     * first run. Only an explicit backend is persisted.
     */
    fun setSelection(context: Context, selection: BackendSelection) {
        _selectionFlow.value = selection
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        if (selection.isOverride) {
            prefs.edit().putString(KEY_BACKEND_OVERRIDE, selection.name).apply()
        } else {
            prefs.edit().remove(KEY_BACKEND_OVERRIDE).apply()
        }
        applySelection()
    }

    /**
     * Re-resolve the backend after pairing or removal changed what is paired.
     *
     * An explicit override stands untouched, including when the backend it
     * names is the one that just went away: that is the state A2's Cancel
     * exists to get out of, and quietly moving the rider to the other backend
     * would hide it. iOS keeps `.unpaired` and sets `pairedFallback` instead
     * (`Shell/SessionGate.swift:434`).
     */
    fun refreshBackend() {
        if (_selectionFlow.value.isOverride) return
        applySelection()
    }

    private fun applySelection() {
        _dataSourceFlow.value = resolveBackend(_selectionFlow.value, session?.isPaired == true, localClient?.isPaired == true)
    }

    suspend fun pairCloud(context: Context, code: String, label: String?) = withContext(Dispatchers.IO) {
        ensureStarted(context).await()
        val s = session ?: throw CloudSession.Failure.NotPaired()
        s.pair(code, label).also { refreshBackend() }
    }

    suspend fun removeCloudDevice(context: Context): RemoveDeviceResult = withContext(Dispatchers.IO) {
        ensureStarted(context).await()
        val s = session ?: throw CloudSession.Failure.NotPaired()
        try {
            s.removeDevice()
        } catch (e: CloudSession.Failure.DeviceRemoved) {
            s.signOut()
            RemoveDeviceResult.LocalFallback
        } finally {
            refreshBackend()
        }
    }

    suspend fun listCloudDevices(context: Context): List<CloudDevice> = withContext(Dispatchers.IO) {
        ensureStarted(context).await()
        val c = client ?: return@withContext emptyList()
        val dev = try {
            credentialStore?.load()
        } catch (_: Exception) {
            null
        } ?: return@withContext emptyList()
        try {
            when (val res = c.devices(dev)) {
                is CloudApiResult.Success -> res.value
                is CloudApiResult.Failure -> emptyList()
            }
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            emptyList()
        }
    }

    suspend fun pairLocal(context: Context, serverUrl: String, token: String): LocalCredentials = withContext(Dispatchers.IO) {
        ensureStarted(context).await()
        val lc = localClient ?: error("local client unavailable")
        val store = credentialStore ?: error("credential store unavailable")

        lc.reset()
        val validUrl = LocalClient.validateServerUrl(serverUrl, isDebug = BuildConfig.DEBUG)
        val creds = LocalCredentials(serverUrl = validUrl, token = token.trim())

        try {
            lc.authenticate(creds)
            store.saveLocal(creds)
            refreshBackend()
            creds
        } catch (e: Exception) {
            // A pairing that did not work persists nothing (iOS test 37):
            // `refreshBackend` stays on the success path only.
            lc.reset()
            throw e
        }
    }

    suspend fun removeLocalDevice(context: Context) = withContext(Dispatchers.IO) {
        ensureStarted(context).await()
        val lc = localClient ?: error("local client unavailable")
        val store = credentialStore ?: error("credential store unavailable")

        lc.reset()
        // `clearLocal()` can refuse (#459 M1): a vault it cannot read or a key
        // it cannot use leaves the file alone instead of deleting it. That is a
        // `CredentialStoreException`, which Settings already renders as "could
        // not save local credentials" rather than a silent success.
        store.clearLocal()
        refreshBackend()
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
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        cloudFailure = null

        // The rider's choice, from the A2 key. `active_data_source` held a
        // DataSource name and was the only choice ever stored; like iOS's
        // `consumeLegacyBackend` (`SessionGate.swift:153-156`) the old value is
        // read, deleted, and never selects a backend. No release build ever
        // shipped the picker, so what is on disk is a developer or emulator
        // choice -- the same class of waiver the owner accepted for #457's
        // missing migration: drop it, and say so.
        prefs.edit().remove(KEY_LEGACY_DATA_SOURCE).apply()
        val storedOverride = prefs.getString(KEY_BACKEND_OVERRIDE, null)
            ?.let { name -> BackendSelection.entries.firstOrNull { it.name == name && it.isOverride } }

        val db = openDatabase(context)
        val credentials = openCredentialStore(context)
        val local = LocalClient(
            credentialsProvider = { credentials.loadLocal() },
            onRevoked = { credentials.clearLocal() },
        )

        // The cloud is one backend, not the app. Building it needs a signing
        // key, and a Keystore that will not hand one over is a cloud that
        // cannot serve -- not a reason to lose the desktop pairing, the cache
        // or the vault this launch did open. iOS splits it the same way
        // (`SessionGate.swift:292-301`): a local credential store is an
        // optional second backend, and the cloud failure is what survives as
        // the launch error only when both are unusable.
        var cloudSession: CloudSession? = null
        var cloudClient: CloudClient? = null
        var signingKey: DeviceKeyStore.DeviceKey? = null
        try {
            val key = openSigner()
            val client = CloudClient(
                baseScheme = BuildConfig.WATTRACKER_CLOUD_SCHEME,
                baseAuthority = BuildConfig.WATTRACKER_CLOUD_AUTHORITY,
                signer = key.signer,
                transport = HttpUrlCloudTransport(),
            )
            signingKey = key
            cloudClient = client
            cloudSession = CloudSession(
                client = client,
                credentials = credentials,
                cache = RoomSnapshotCache(db),
                removalGate = SharedPreferencesRemovalGate(context),
            )
        } catch (t: Throwable) {
            if (t is CancellationException) throw t
            cloudFailure = t
        }

        database = db
        this.client = cloudClient
        this.session = cloudSession
        this.localClient = local
        this.credentialStore = credentials
        this.deviceKey = signingKey
        this.readModel = ReadModel(
            cloud = cloudSession,
            local = local,
            source = { _dataSourceFlow.value },
        )

        // An override for a backend that is not paired is dropped here, once,
        // so it cannot pin the app to a pairing screen while the other backend
        // works (`SessionGate.swift:305-312`).
        val cloudPaired = cloudSession?.isPaired == true
        val localPaired = local.isPaired
        val selection = if (storedOverride != null && keepOverride(storedOverride, cloudPaired, localPaired)) {
            storedOverride
        } else {
            if (storedOverride != null) prefs.edit().remove(KEY_BACKEND_OVERRIDE).apply()
            BackendSelection.Automatic
        }
        _selectionFlow.value = selection
        _dataSourceFlow.value = resolveBackend(selection, cloudPaired, localPaired)
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
        dropLegacyEncryptedStore(context)
        return try {
            // `open` loads the Keystore key and then *drives* it -- one
            // throwaway encrypt/decrypt through the production cipher -- before
            // a store exists. So both ways a key fails to arrive reach this
            // catch: no key at all, and a key the Keystore will not let anyone
            // use (a corrupt key blob after an OEM update, say). The second one
            // used to slip through, because holding a handle looked like having
            // a key: the app reported unpaired, refused every save after a
            // pairing code had been spent, and nothing short of clearing app
            // data helped. Where there is no vault on disk to lose, the store
            // replaces the dead alias first and only then gives up here.
            KeystoreDeviceCredentialStore.open(context)
        } catch (_: Exception) {
            // The Keystore itself will not take part: it hands over no AES key,
            // or it hands over one whose bytes can be read, which this app
            // refuses -- and without a key nothing can be wrapped. Start
            // unpaired from memory rather than bricking the app: there is no
            // credential to protect that is not also unusable. The fallback is
            // not silent either -- the in-memory store reports
            // `StoreHealth.Unavailable`, so the pairing screen can say this
            // launch is all a pairing here lasts.
            InMemoryDeviceCredentialStore()
        }
    }

    /**
     * #457 took the credential store off `security-crypto`, and with that library
     * gone nothing here can read the prefs file it wrote. One file, not two:
     * `EncryptedSharedPreferences` kept its Tink keysets inside that same file
     * under the reserved keys `__androidx_security_crypto_encrypted_prefs_*`
     * (verified against the library's own source), and the master key was a plain
     * Keystore entry with no prefs of its own.
     *
     * Nothing ever shipped it -- no release has carried the encrypted store, and
     * #199's internal testing starts on this branch -- so there is nothing to
     * migrate and no rider re-pairs; what exists is dead bytes on developer and
     * emulator installs, so it is dropped. Idempotent, and this call can go once
     * no such install is in use.
     *
     * The orphan Keystore entry that guarded those keysets is deliberately left
     * alone: it cannot be exported, it protects nothing now, and the Keystore
     * takes it back with the app. Naming another library's private alias to delete
     * it is a coupling worth less than the residue it clears.
     */
    private fun dropLegacyEncryptedStore(context: Context) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        if (prefs.getBoolean(KEY_LEGACY_STORE_DROPPED, false)) return
        context.deleteSharedPreferences(LEGACY_CREDENTIAL_PREFS)
        prefs.edit().putBoolean(KEY_LEGACY_STORE_DROPPED, true).apply()
    }

    private fun openSigner(): DeviceKeyStore.DeviceKey {
        // A missing key is generated on first run. If the Keystore itself will
        // not hand one over this throws, and the caller treats it as a cloud
        // that cannot serve for this launch: nothing can be signed, so nothing
        // can be read from the cloud or paired to it. The desktop backend, the
        // vault and the cache stay open -- only the app-level gate (A1) gets
        // to turn this into "this device cannot be paired".
        return DeviceKeyStore().loadOrCreate()
    }

    private const val PREFS_NAME = "wattracker_app_prefs"

    /** The A2 rider choice. Absent means `Automatic`, and `Automatic` is never written. */
    private const val KEY_BACKEND_OVERRIDE = "active_backend_selection"

    /** What the pre-A2 picker stored: a [DataSource] name. Consumed and deleted. */
    private const val KEY_LEGACY_DATA_SOURCE = "active_data_source"
    private const val KEY_LEGACY_STORE_DROPPED = "legacy_credential_store_dropped"

    /** The prefs file the security-crypto store wrote; see [dropLegacyEncryptedStore]. */
    private const val LEGACY_CREDENTIAL_PREFS = "wattracker_device_credential"
}
