package com.wattracker.android.cloud

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * The key that wraps the credential file, plus the premise every user of it
 * depends on: the key cannot be exported.
 *
 * Two Keystore keys serve this app and they answer different questions. The
 * EC P-256 pair in [DeviceKeyStore] answers "which device is signing" -- losing
 * it costs one pairing code, so it reaches for StrongBox first. This one answers
 * "who may read this file", and losing it costs the rider their pairing until
 * they type a code again, so it deliberately asks for nothing stronger than the
 * plain Keystore: StrongBox has a handful of key slots and its own lifetime
 * rules, and a credential key that fails to be created -- or stops resolving
 * after an update -- unpairs a working device. Non-exportable is the property
 * that carries the security here, and the plain Keystore gives it.
 *
 * The key is created on first use and is never written anywhere, which is the
 * other half of why the credential file may never leave the device: the blob
 * and the key are two halves of one installation. That is what the manifest's
 * backup rules keep together and what [AesGcmCipher]'s associated data makes
 * expensive even if a file is moved by hand.
 */
object CredentialKeyStore {

    /** Thrown when the Keystore will not hand over a usable, non-exportable key. */
    class CredentialKeyException(message: String, cause: Throwable? = null) : Exception(message, cause)

    /**
     * The credential key, created on first call.
     *
     * The key returned is the one read back **through** the Keystore, not the
     * in-memory object generation handed back: the persisted key is the one
     * every later launch will use, and a write wrapped under anything else is a
     * credential the next launch cannot open. [DeviceKeyStore] takes the same
     * posture for the signing key, for the same reason.
     */
    fun loadOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(ALIAS, null) as? SecretKey)?.let { existing ->
            assertNonExportable(existing)
            return existing
        }

        val generated = generate()
        val stored = (keyStore.getKey(ALIAS, null) as? SecretKey) ?: generated
        assertNonExportable(stored)
        return stored
    }

    /**
     * A 256-bit AES-GCM key that the provider randomizes the IV for. The random
     * IV is not a nicety: GCM breaks on a repeated (key, IV), and asking the
     * platform to own the IV is what stops this app from ever being the one to
     * repeat it.
     */
    private fun generate(): SecretKey {
        val generator = try {
            KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        } catch (e: Exception) {
            throw CredentialKeyException("the Android Keystore is unavailable", e)
        }
        val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .setRandomizedEncryptionRequired(true)
            // The pairing code is the authorization; a biometric gate on reading
            // one's own credential would be wrong for a phone on a bike, and it
            // is not what #194's threat model asked for.
            .setUserAuthenticationRequired(false)
            .build()
        return try {
            generator.init(spec)
            generator.generateKey()
        } catch (e: Exception) {
            throw CredentialKeyException("could not create the credential key", e)
        }
    }

    /**
     * A Keystore key cannot be exported: [SecretKey.getEncoded] returns null or
     * throws. A non-null encoding would mean the key protecting the credential
     * file is sitting in memory the whole time, and the premise is gone -- so
     * the store refuses to use such a key rather than quietly being weaker than
     * it claims.
     */
    private fun assertNonExportable(key: SecretKey) {
        val encoded: ByteArray? = try {
            key.encoded
        } catch (_: Exception) {
            null
        }
        if (encoded != null) {
            throw CredentialKeyException("the credential key is exportable; refusing to use it")
        }
    }

    /** The Keystore alias. Changing it orphans every paired device's file. */
    const val ALIAS = "com.wattracker.android.device-credential-key"

    private const val KEYSTORE = "AndroidKeyStore"
}
