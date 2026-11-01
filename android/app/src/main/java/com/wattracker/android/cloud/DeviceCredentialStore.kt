package com.wattracker.android.cloud

import android.content.Context
import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.opt
import com.wattracker.android.json.optDouble
import com.wattracker.android.json.optInt
import com.wattracker.android.json.optString
import com.wattracker.android.json.toJson
import java.io.File
import java.io.FileOutputStream
import javax.crypto.SecretKey

/**
 * Storage for the cloud device binding and local desktop credentials.
 *
 * The interface exists so the state machine can be exercised on the JVM with
 * no Android in it; production uses [KeystoreDeviceCredentialStore]. The
 * bearer secrets are never logged and never written in the clear -- that is an
 * invariant the #194 acceptance checks, not a preference.
 */
interface DeviceCredentialStore {
    fun load(): PairedDevice?
    fun save(device: PairedDevice)
    fun clear()

    fun loadLocal(): LocalCredentials?
    fun saveLocal(credentials: LocalCredentials)
    fun clearLocal()

    /**
     * What this store can promise about the next launch.
     *
     * #195's pairing screen must not let a rider pair into a store that will
     * forget them, and the two ways a store fails are worth different copy: one
     * means nothing can be written durably at all, the other means a pairing is
     * already lost and pairing again fixes it. So this stays a question the app
     * can ask, not a detail of storage.
     */
    val health: StoreHealth
        get() = StoreHealth.Durable
}

/** What a store promises about the next launch. */
enum class StoreHealth {
    /** What is written here is here next launch. */
    Durable,

    /** Nothing can be wrapped at all: the store is in memory and a pairing ends with the process. */
    Unavailable,

    /**
     * The key works and the store can write, but the vault already on disk
     * would not open under it. The saved pairing is not in use; a new one
     * replaces it.
     */
    UnreadableVault,
}

/** A credential the store could not write, clear, or delete durably. */
class CredentialStoreException(message: String, cause: Throwable? = null) : Exception(message, cause)

/** An in-process store, for tests and for holding the pre-pairing state. */
class InMemoryDeviceCredentialStore : DeviceCredentialStore {

    @Volatile
    private var stored: PairedDevice? = null

    @Volatile
    private var storedLocal: LocalCredentials? = null

    /** A pairing made here lasts one launch, which is what [StoreHealth.Unavailable] means. */
    override val health: StoreHealth get() = StoreHealth.Unavailable

    override fun load(): PairedDevice? = stored
    override fun save(device: PairedDevice) {
        stored = device
    }
    override fun clear() {
        stored = null
    }

    override fun loadLocal(): LocalCredentials? = storedLocal
    override fun saveLocal(credentials: LocalCredentials) {
        storedLocal = credentials
    }
    override fun clearLocal() {
        storedLocal = null
    }
}

/**
 * The production store: the cloud device binding and the desktop connector token
 * as one JSON vault, wrapped by a non-exportable Android Keystore AES-256-GCM key
 * ([CredentialKeyStore]) and written to a file under `filesDir`.
 *
 * It replaces the Tink-backed `EncryptedSharedPreferences` store (#457), whose
 * library is deprecated and will not be patched again. The library was never the
 * security here -- the Keystore-wrapped key behind it was -- and the Keystore
 * gives that directly, so the app now depends on no crypto of its own choosing.
 *
 * Durability keeps the shape the #304 review specified, in file terms: every
 * write is `temp file -> fsync -> rename`, and a write the filesystem refuses is
 * reported rather than ignored, because the session's "cache first, credential
 * second" ordering only holds if this write survives the crash right after it.
 * A rename within a directory is atomic, so a reader -- the next launch
 * included -- sees the whole old vault or the whole new one; the directory entry
 * is not fsynced, which can lose the rename and leave the old vault, never half
 * a new one.
 *
 * **A read is three-way, not two.** The #459 review caught the old shape: any
 * failure to read was flattened to an empty vault, and the next `clear()` or
 * `save()` then rewrote the file -- so one transient I/O error during a desktop
 * 401 could delete a still-valid cloud pairing. The distinction that matters is
 * *why* the read failed:
 *
 * - **The file could not be read** (`IOException`, permission, a half-written
 *   temp file): the vault may be perfectly good. Nothing here may act on it --
 *   `load()` answers `null` for this attempt, and every mutation throws
 *   [CredentialStoreException] and leaves the file exactly where it is.
 * - **The key could not be used**: same rule, plus [StoreHealth.Unavailable],
 *   which is the difference between "pair again and it is fixed" and "this
 *   device cannot save a pairing at all".
 * - **The envelope will not open** (another key, a tampered byte, a foreign
 *   purpose, an unknown format): GCM has certified these bytes are not a
 *   credential this installation wrote. A *save* may replace it outright, since
 *   a fresh pairing is the only way back; a *clear* must not, because deleting
 *   on that signal turns "unreadable" into "gone" and loses nothing recoverable
 *   but gains nothing either. The file stays and [health] reports
 *   [StoreHealth.UnreadableVault] until a read succeeds.
 *
 * A member this build cannot parse -- a field added by a newer server -- is
 * **kept as raw JSON** rather than dropped: the other member is still served,
 * and the next write re-emits the unreadable member unchanged instead of
 * silently deleting it.
 *
 * The decoded vault is cached under [lock] and replaced on every successful
 * write (#459 review, L1): `loadLocal()` is the desktop connector's
 * per-request path, so a disk read plus a Keystore IPC round trip per request
 * was waste. One writer owns one file, so the cache cannot go stale behind the
 * store's back.
 *
 * The key is taken in the constructor for the fallback reason above:
 * [CredentialKeyStore.loadOrCreateKey] has to be able to throw while the app is
 * still choosing a store, where the answer is to fall back and say so, and not
 * on the first write after a pairing code has already been spent.
 */
class KeystoreDeviceCredentialStore internal constructor(
    private val file: File,
    private val cipher: CredentialCipher,
) : DeviceCredentialStore {

    constructor(context: Context) : this(
        File(context.filesDir, FILE_NAME),
        AesGcmCipher(
            // Eager, and taken once. A Keystore that will not hand over a key
            // has to throw here, while the app is still choosing a store
            // (WatTrackerApplication.openCredentialStore): handed to the cipher
            // as a lazy lambda, that fallback could never fire and the failure
            // would arrive on the write that follows a spent pairing code.
            // One read per launch rather than one per operation because the
            // alias is fixed for the life of the process.
            keyProvider = eagerKeyProvider { CredentialKeyStore.loadOrCreateKey() },
            associatedData = ASSOCIATED_DATA.toByteArray(Charsets.UTF_8),
        ),
    )

    /** One vault, one file, one writer: the two credentials never race each other. */
    private val lock = Any()

    /** The last vault read or written; `null` means "not looked at yet this launch". */
    private var cachedVault: Vault? = null

    @Volatile
    private var vaultUnreadable = false

    /**
     * Sticky for this launch once anything says the *key* is the problem rather
     * than the file. Not cleared by a later success: the store holds one key
     * handle, so a success after a key failure would mean the first failure was
     * an oddity, and the honest answer to that is a relaunch, not a promise of
     * durability.
     */
    @Volatile
    private var keyUnavailable = false

    /**
     * [StoreHealth.Unavailable] if the key could not be used at all, else
     * [StoreHealth.UnreadableVault] while the last look at [file] found an
     * envelope this installation's key could not open. The vault flag is set by
     * a read and cleared by the next read or write that succeeds; the key flag
     * is not cleared, because this launch has no second key to try.
     *
     * A *transient* read failure deliberately does not change this verdict: it
     * is a failed operation, not evidence about the vault, and the copy attached
     * to [StoreHealth.UnreadableVault] ("pairing again will replace it") would be
     * wrong advice for a file that may open on the next attempt.
     */
    override val health: StoreHealth
        get() = when {
            keyUnavailable -> StoreHealth.Unavailable
            vaultUnreadable -> StoreHealth.UnreadableVault
            else -> StoreHealth.Durable
        }

    override fun load(): PairedDevice? = readVault().pairedDevice.parsed

    override fun loadLocal(): LocalCredentials? = readVault().localCredentials.parsed

    // Durable by design: `commit()` was the old store's word for it, and the
    // word for it here is fsync plus a rename whose failure is thrown.
    override fun save(device: PairedDevice) =
        mutateVault(mayReplaceUnopenable = true) { it.copy(pairedDevice = Slot.of(device)) }

    override fun clear() =
        mutateVault(mayReplaceUnopenable = false) { it.copy(pairedDevice = Slot()) }

    override fun saveLocal(credentials: LocalCredentials) =
        mutateVault(mayReplaceUnopenable = true) { it.copy(localCredentials = Slot.of(credentials)) }

    override fun clearLocal() =
        mutateVault(mayReplaceUnopenable = false) { it.copy(localCredentials = Slot()) }

    /** One vault member: a model this build reads, or the raw JSON it could not. */
    private data class Slot<T>(val parsed: T? = null, val raw: JsonValue? = null) {
        fun json(serialize: (T) -> JsonValue): JsonValue? = parsed?.let(serialize) ?: raw

        companion object {
            fun <T> of(model: T?): Slot<T> = Slot(model, null)
        }
    }

    /** The two credentials of one installation, in one envelope. */
    private data class Vault(
        val pairedDevice: Slot<PairedDevice> = Slot(),
        val localCredentials: Slot<LocalCredentials> = Slot(),
    ) {
        val isEmpty: Boolean
            get() = (pairedDevice.parsed == null && pairedDevice.raw == null) &&
                (localCredentials.parsed == null && localCredentials.raw == null)
    }

    /** What one look at [file] established. */
    private sealed interface ReadResult {
        /** The vault is there (or the file is not, which is an empty vault). */
        data class Opened(val vault: Vault) : ReadResult

        /** The envelope is on disk but this installation cannot open it. */
        object Unopenable : ReadResult

        /** The file exists and could not be read; nothing may be concluded. */
        object ReadFailed : ReadResult

        /** There is no key to ask. */
        object NoKey : ReadResult
    }

    /**
     * [mayReplaceUnopenable] is true only for a save: a fresh pairing is the only
     * way back from a vault this installation cannot open, so it may replace it.
     * A clear must not -- deleting on that signal turns "unreadable" into
     * "gone", which recovers nothing and loses the file a later launch might
     * still open.
     */
    private fun mutateVault(mayReplaceUnopenable: Boolean, change: (Vault) -> Vault) {
        synchronized(lock) {
            val base = when (val result = readVaultLocked()) {
                is ReadResult.Opened -> result.vault
                // A read that failed tells us nothing about the credential, so
                // acting on it would be inventing an empty vault and destroying
                // whatever is actually in the file. The session handles a refused
                // write; this is the refused read it must not mistake for empty.
                ReadResult.ReadFailed ->
                    throw CredentialStoreException("the credential file could not be read; nothing was changed")
                ReadResult.NoKey ->
                    throw CredentialStoreException("the credential key could not be used; nothing was changed")
                ReadResult.Unopenable -> if (!mayReplaceUnopenable) return else Vault()
            }

            val updated = change(base)
            if (updated.isEmpty) {
                deleteVaultLocked()
            } else {
                writeVaultLocked(updated)
            }
        }
    }

    private fun readVault(): Vault = synchronized(lock) {
        when (val result = readVaultLocked()) {
            is ReadResult.Opened -> result.vault
            // Unpaired is the answer the app needs in every failure case; the
            // reason is carried by `health`, and by the refused mutation above.
            ReadResult.Unopenable, ReadResult.ReadFailed, ReadResult.NoKey -> Vault()
        }
    }

    /** The caller holds [lock]. Cached after the first look, so one launch reads the file once. */
    private fun readVaultLocked(): ReadResult {
        cachedVault?.let { return ReadResult.Opened(it) }

        val envelope = try {
            if (!file.exists()) {
                vaultUnreadable = false
                return ReadResult.Opened(Vault().also { cachedVault = it })
            }
            file.readBytes()
        } catch (_: Exception) {
            // Transient until proven otherwise: the file is left alone, the
            // verdict on the vault does not change, and the next read answers
            // again. A mutation on top of this attempt is refused, not applied.
            return ReadResult.ReadFailed
        }

        val vault = try {
            decode(envelope)
        } catch (_: CredentialKeyStore.CredentialKeyException) {
            // No key to ask, which is not the same message as a vault that
            // answers badly: "pair again replaces it" is a lie on a device that
            // cannot write a pairing either.
            keyUnavailable = true
            return ReadResult.NoKey
        } catch (_: Exception) {
            // GCM said these bytes are not a credential this key wrote, or the
            // format is not one this build describes. Either way it is a
            // verdict about the file, not a transient failure.
            vaultUnreadable = true
            return ReadResult.Unopenable
        }
        vaultUnreadable = false
        cachedVault = vault
        return ReadResult.Opened(vault)
    }

    private fun decode(envelope: ByteArray): Vault {
        val json = JsonValue.parse(String(cipher.decrypt(envelope), Charsets.UTF_8))
        val format = json.optInt("format")
        if (format != FORMAT_VERSION) {
            throw CredentialCipherException("the credential vault is format ${format ?: "unknown"}, not $FORMAT_VERSION")
        }
        // The field-level JSON stays PairedDevice's and LocalCredentials' own --
        // this file only nests it, so each encoding has exactly one author. A
        // member this model cannot read is kept as the raw JSON it arrived as
        // (a field a newer server added), so the other member is still served and
        // the next write re-emits it unchanged instead of dropping it.
        return Vault(
            pairedDevice = slot(json.opt("paired_device")) { PairedDevice.fromJson(it) },
            localCredentials = slot(json.opt("local_credentials")) { LocalCredentials.fromJson(it) },
        )
    }

    private fun <T> slot(raw: JsonValue?, parse: (String) -> T?): Slot<T> {
        if (raw == null) return Slot()
        val parsed = runCatching { parse(raw.toJson()) }.getOrNull()
        return if (parsed == null) Slot(parsed, raw) else Slot.of(parsed)
    }

    private fun encode(vault: Vault): ByteArray {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["format"] = JsonValue.Number(FORMAT_VERSION.toDouble())
        vault.pairedDevice.json { JsonValue.parse(it.toJson()) }?.let { fields["paired_device"] = it }
        vault.localCredentials.json { JsonValue.parse(it.toJson()) }?.let { fields["local_credentials"] = it }
        return JsonValue.Object(fields).toJson().toByteArray(Charsets.UTF_8)
    }

    private fun writeVaultLocked(vault: Vault) {
        val envelope = try {
            cipher.encrypt(encode(vault))
        } catch (e: CredentialKeyStore.CredentialKeyException) {
            // The key, not the filesystem: reported as a refused write (the
            // session already handles that) and remembered as Unavailable, so
            // the screen can say the pairing will not outlive this launch.
            keyUnavailable = true
            throw CredentialStoreException("the credential key could not wrap the vault", e)
        }
        val temp = File(file.parentFile, file.name + TEMP_SUFFIX)
        try {
            FileOutputStream(temp).use { out ->
                out.write(envelope)
                out.flush()
                out.fd.sync()
            }
            if (!temp.renameTo(file)) {
                throw CredentialStoreException("could not replace the credential file")
            }
            vaultUnreadable = false
            cachedVault = vault
        } catch (e: CredentialStoreException) {
            throw e
        } catch (e: Exception) {
            throw CredentialStoreException("could not write the credential file", e)
        } finally {
            // A successful rename consumed it already; this only cleans up a
            // failed one, and a leftover temp file protects nothing.
            temp.delete()
        }
    }

    private fun deleteVaultLocked() {
        if (!file.exists()) {
            vaultUnreadable = false
            cachedVault = Vault()
            return
        }
        if (!file.delete()) {
            throw CredentialStoreException("could not delete the credential file")
        }
        vaultUnreadable = false
        cachedVault = Vault()
    }

    companion object {
        /** The `files/` file name -- no longer a `shared_prefs` name. */
        const val FILE_NAME = "wattracker_device_credentials"

        /**
         * Authenticated, not secret, and one constant shared by every install:
         * it separates this file's purpose from any other use of the same key,
         * so an envelope written for something else does not open. It is **not**
         * what binds a vault to an installation -- that is the per-installation
         * Keystore key, which never leaves the device and is what a carried or
         * restored file lacks.
         */
        private const val ASSOCIATED_DATA = "com.wattracker.android.device-credentials-v1"

        private const val FORMAT_VERSION = 1
        private const val TEMP_SUFFIX = ".tmp"
    }
}

/**
 * Takes the key **now** and hands back a provider for the held handle.
 *
 * Both halves are the point, and both are tested: the load happens when this
 * function is called -- so a store whose key is unavailable fails during
 * construction, where its caller can still choose a fallback -- and it happens
 * once however many operations follow, because a Keystore round trip per
 * `loadLocal()` is waste on a fixed alias.
 */
internal fun eagerKeyProvider(load: () -> SecretKey): () -> SecretKey {
    val key = load()
    return { key }
}

fun LocalCredentials.toJson(): String {
    val fields = mutableMapOf<String, JsonValue>(
        "serverUrl" to JsonValue.String(serverUrl),
        "token" to JsonValue.String(token),
        "pairedAtMillis" to JsonValue.Number(pairedAtMillis.toDouble()),
    )
    if (username != null) {
        fields["username"] = JsonValue.String(username)
    }
    return JsonValue.Object(fields).toJson()
}

fun LocalCredentials.Companion.fromJson(jsonText: String): LocalCredentials? {
    val json = runCatching { JsonValue.parse(jsonText) }.getOrNull() as? JsonValue.Object ?: return null
    val url = json.optString("serverUrl") ?: return null
    val tok = json.optString("token") ?: return null
    val user = json.optString("username")
    val pairedAt = json.optDouble("pairedAtMillis")?.toLong() ?: System.currentTimeMillis()
    return LocalCredentials(url, tok, user, pairedAt)
}
