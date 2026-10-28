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
 * A vault that will not open -- another key, a tampered byte, a file carried in
 * from another installation -- is left exactly where it is and reported through
 * [health] as [StoreHealth.UnreadableVault]: the rider is unpaired, and the next
 * pairing overwrites the file. Deleting it would destroy a credential that may
 * yet open (one Keystore hiccup is not a credential that is gone), and answering
 * `null` in silence would look exactly like "never paired".
 *
 * A vault that cannot be opened *because there is no key* says
 * [StoreHealth.Unavailable] instead, which is the difference between "pair again
 * and it is fixed" and "this device cannot save a pairing at all". The key is
 * taken in the constructor for that reason: [CredentialKeyStore.loadOrCreateKey]
 * has to be able to throw while the app is still choosing a store, where the
 * answer is to fall back and say so, and not on the first write after a pairing
 * code has already been spent.
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
            ivSource = AesGcmCipher.IvSource.PROVIDER_GENERATED,
        ),
    )

    /** One vault, one file, one writer: the two credentials never race each other. */
    private val lock = Any()

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
     * [StoreHealth.UnreadableVault] while the last look at [file] found
     * something this installation's key could not open. The vault flag is set by
     * a read and cleared by the next read that succeeds -- including the one a
     * fresh pairing writes; the key flag is not cleared, because this launch has
     * no second key to try.
     */
    override val health: StoreHealth
        get() = when {
            keyUnavailable -> StoreHealth.Unavailable
            vaultUnreadable -> StoreHealth.UnreadableVault
            else -> StoreHealth.Durable
        }

    override fun load(): PairedDevice? = readVault().pairedDevice

    override fun loadLocal(): LocalCredentials? = readVault().localCredentials

    // Durable by design: `commit()` was the old store's word for it, and the
    // word for it here is fsync plus a rename whose failure is thrown.
    override fun save(device: PairedDevice) = mutateVault { it.copy(pairedDevice = device) }

    override fun clear() = mutateVault { it.copy(pairedDevice = null) }

    override fun saveLocal(credentials: LocalCredentials) = mutateVault { it.copy(localCredentials = credentials) }

    override fun clearLocal() = mutateVault { it.copy(localCredentials = null) }

    /** The two credentials of one installation, in one envelope. */
    private data class Vault(
        val pairedDevice: PairedDevice? = null,
        val localCredentials: LocalCredentials? = null,
    )

    private fun mutateVault(change: (Vault) -> Vault) {
        synchronized(lock) {
            // An unreadable vault changes into an empty one, so a pairing over a
            // dead file replaces it outright. Nothing recoverable is lost: GCM
            // has already certified that the bytes are not a credential.
            val updated = change(readVaultLocked())
            if (updated.pairedDevice == null && updated.localCredentials == null) {
                deleteVaultLocked()
            } else {
                writeVaultLocked(updated)
            }
        }
    }

    private fun readVault(): Vault = synchronized(lock) { readVaultLocked() }

    /** The caller holds [lock]. No file and an unopenable file both read as "no credential". */
    private fun readVaultLocked(): Vault {
        val envelope = try {
            if (!file.exists()) {
                vaultUnreadable = false
                return Vault()
            }
            file.readBytes()
        } catch (_: Exception) {
            // The file was there and would not read. That is more often a
            // transient I/O failure than a corrupted vault, which is why the
            // file is left alone and this clears on the next successful read --
            // the next pull-to-refresh answers it correctly.
            vaultUnreadable = true
            return Vault()
        }
        return try {
            decode(envelope).also { vaultUnreadable = false }
        } catch (_: CredentialKeyStore.CredentialKeyException) {
            // No key to ask, which is not the same message as a vault that
            // answers badly: "pair again replaces it" is a lie on a device that
            // cannot write a pairing either.
            keyUnavailable = true
            Vault()
        } catch (_: Exception) {
            vaultUnreadable = true
            Vault()
        }
    }

    private fun decode(envelope: ByteArray): Vault {
        val json = JsonValue.parse(String(cipher.decrypt(envelope), Charsets.UTF_8))
        val format = json.optInt("format")
        if (format != FORMAT_VERSION) {
            throw CredentialCipherException("the credential vault is format ${format ?: "unknown"}, not $FORMAT_VERSION")
        }
        // The field-level JSON stays PairedDevice's and LocalCredentials' own --
        // this file only nests it, so each encoding has exactly one author. A
        // member this model cannot read is that member missing (the posture the
        // store has always taken), and the other one is still served.
        val device = json.opt("paired_device")?.let { runCatching { PairedDevice.fromJson(it.toJson()) }.getOrNull() }
        val local = json.opt("local_credentials")?.let { runCatching { LocalCredentials.fromJson(it.toJson()) }.getOrNull() }
        return Vault(device, local)
    }

    private fun encode(vault: Vault): ByteArray {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["format"] = JsonValue.Number(FORMAT_VERSION.toDouble())
        vault.pairedDevice?.let { fields["paired_device"] = JsonValue.parse(it.toJson()) }
        vault.localCredentials?.let { fields["local_credentials"] = JsonValue.parse(it.toJson()) }
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
            return
        }
        if (!file.delete()) {
            throw CredentialStoreException("could not delete the credential file")
        }
        vaultUnreadable = false
    }

    companion object {
        /** The `files/` file name -- no longer a `shared_prefs` name. */
        const val FILE_NAME = "wattracker_device_credentials"

        /**
         * Authenticated, not secret, and never shared between two files: an
         * envelope is bound to the vault it was written for, so a credential
         * file copied over another one -- or restored into a different
         * installation and renamed -- does not open even on a device whose key
         * it would otherwise serve.
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
