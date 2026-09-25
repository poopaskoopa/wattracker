package com.wattracker.android.cloud

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Storage for the cloud device binding.
 *
 * The interface exists so the state machine can be exercised on the JVM with
 * no Android in it; production uses [EncryptedDeviceCredentialStore]. The
 * bearer secrets are never logged and never written to plain
 * `SharedPreferences` -- that is an invariant the #194 acceptance checks, not
 * a preference.
 */
interface DeviceCredentialStore {
    fun load(): PairedDevice?
    fun save(device: PairedDevice)
    fun clear()
}

/** A credential the encrypted store could not write or clear durably. */
class CredentialStoreException(message: String) : Exception(message)

/** An in-process store, for tests and for holding the pre-pairing state. */
class InMemoryDeviceCredentialStore : DeviceCredentialStore {

    @Volatile
    private var stored: PairedDevice? = null

    override fun load(): PairedDevice? = stored
    override fun save(device: PairedDevice) {
        stored = device
    }
    override fun clear() {
        stored = null
    }
}

/**
 * The production store: `PairedDevice` JSON inside Tink-encrypted
 * `SharedPreferences`. The key and its master key live in the Android
 * Keystore.
 */
class EncryptedDeviceCredentialStore(context: Context) : DeviceCredentialStore {

    private val prefs: SharedPreferences = createEncrypted(context, FILE_NAME)

    override fun load(): PairedDevice? =
        prefs.getString(KEY, null)?.let { runCatching { PairedDevice.fromJson(it) }.getOrNull() }

    // Durable by design: `commit()` (not `apply()`) and the result is checked.
    // The "cache first, credential second" ordering the session relies on only
    // holds if this write survives a crash right after it.
    override fun save(device: PairedDevice) {
        if (!prefs.edit().putString(KEY, device.toJson()).commit()) {
            throw CredentialStoreException("could not persist the device credential")
        }
    }

    override fun clear() {
        if (!prefs.edit().remove(KEY).commit()) {
            throw CredentialStoreException("could not clear the device credential")
        }
    }

    companion object {
        private const val KEY = "paired_device"

        /** The `shared_prefs` file name, so a caller can reset this store by name. */
        const val FILE_NAME = "wattracker_device_credential"

        internal fun createEncrypted(context: Context, fileName: String): SharedPreferences {
            val masterKey = MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            return EncryptedSharedPreferences.create(
                context,
                fileName,
                masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
        }
    }
}
