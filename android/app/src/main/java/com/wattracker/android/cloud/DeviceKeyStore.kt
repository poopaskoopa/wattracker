package com.wattracker.android.cloud

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import java.security.InvalidKeyException
import java.security.Key
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey

/**
 * Loads the device's signing key, generating one on first run, and reports
 * where it lives.
 *
 * The private half is a non-exportable P-256 key in the Android Keystore,
 * preferred in **StrongBox** with a plain-Keystore (TEE or software) fallback --
 * not every device has StrongBox, and the pairing code is the authorization
 * (biometrics on every request would be wrong for a phone on a bike). The
 * [KeyKind] is surfaced in Settings per #194's Done criteria; it is reported,
 * never trusted for an authorization decision.
 *
 * The key is persisted by its [ALIAS] in the Keystore, which is the whole
 * storage story -- there is no private scalar to write anywhere else. StrongBox
 * is requested on the *same* `AndroidKeyStore` provider via
 * [KeyGenParameterSpec.Builder.setIsStrongBoxBacked]; there is no separate
 * provider to look up. [assertNonExportable] verifies the non-exportability the
 * rest of this type relies on rather than assuming it.
 */
class DeviceKeyStore {

    /** Where the private half lives, for the Settings display. */
    enum class KeyKind(val label: String) {
        /** StrongBox -- a dedicated security IC, the strongest class. */
        StrongBox("StrongBox"),

        /** Hardware Keystore (TEE) -- not StrongBox, still in hardware. */
        TEE("Hardware Keystore"),

        /** Software Keystore -- hardware absent; the dev-emulator case. */
        Software("Software Keystore"),
    }

    /** A loaded key and where it lives. */
    data class DeviceKey(val signer: DeviceSigner, val kind: KeyKind)

    fun loadOrCreate(): DeviceKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        val existing = keyStore.getKey(ALIAS, null) as? PrivateKey
        if (existing != null) {
            val certificate = keyStore.getCertificate(ALIAS)
                ?: throw KeyStoreException("key present but certificate missing")
            assertNonExportable(existing)
            val keyPair = KeyPair(certificate.publicKey, existing)
            return DeviceKey(KeyPairSigner(keyPair, isHardwareBacked(existing)), kindOf(existing))
        }

        val generated = generate()
        // Re-read through the store so the asserted and signed key is the
        // persisted one, not the in-memory copy that came back from generation.
        val storedPrivate = (keyStore.getKey(ALIAS, null) as? PrivateKey) ?: generated.private
        val certificate = keyStore.getCertificate(ALIAS) ?: throw KeyStoreException("key not persisted")
        assertNonExportable(storedPrivate)
        val storedKeyPair = KeyPair(certificate.publicKey, storedPrivate)
        return DeviceKey(KeyPairSigner(storedKeyPair, isHardwareBacked(storedPrivate)), kindOf(storedPrivate))
    }

    /**
     * A Keystore key cannot be exported: requesting its encoded form returns
     * null or throws [InvalidKeyException]. A non-null encoding would mean the
     * private scalar is sitting on disk and the whole premise is gone.
     */
    private fun assertNonExportable(privateKey: PrivateKey) {
        val encoded: ByteArray? = try {
            privateKey.encoded
        } catch (e: InvalidKeyException) {
            null
        }
        if (encoded != null) {
            throw KeyStoreException("the device signing key is exportable; refusing to use it")
        }
    }

    /** Read [KeyInfo] from the key, or null when the provider cannot supply it. */
    private fun keyInfo(privateKey: Key): KeyInfo? = try {
        KeyFactory.getInstance(KEY_ALGORITHM, KEYSTORE).getKeySpec(privateKey, KeyInfo::class.java)
    } catch (e: Exception) {
        null
    }

    @Suppress("DEPRECATION")
    private fun isHardwareBacked(privateKey: Key): Boolean = keyInfo(privateKey)?.let { info ->
        if (Build.VERSION.SDK_INT >= 31) {
            info.securityLevel >= KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT
        } else {
            info.isInsideSecureHardware
        }
    } ?: false

    @Suppress("DEPRECATION")
    private fun kindOf(privateKey: Key): KeyKind {
        val info = keyInfo(privateKey) ?: return KeyKind.Software
        return if (Build.VERSION.SDK_INT >= 31) {
            when (info.securityLevel) {
                KeyProperties.SECURITY_LEVEL_STRONGBOX -> KeyKind.StrongBox
                KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT -> KeyKind.TEE
                else -> KeyKind.Software
            }
        } else {
            // API 30 has no StrongBox/TEE distinction: hardware is reported as TEE.
            if (info.isInsideSecureHardware) KeyKind.TEE else KeyKind.Software
        }
    }

    /** A P-256 key, StrongBox-preferred with a plain-Keystore fallback. */
    private fun generate(): KeyPair {
        // StrongBox is a flag on the plain provider, not a provider of its own.
        val strongBox = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
            .setKeySize(256)
            .setDigests(KeyProperties.DIGEST_SHA256)
            .setUserAuthenticationRequired(false)
            .setIsStrongBoxBacked(true)
            .build()
        try {
            val generator = KeyPairGenerator.getInstance(KEY_ALGORITHM, KEYSTORE)
            generator.initialize(strongBox)
            return generator.generateKeyPair()
        } catch (e: StrongBoxUnavailableException) {
            // No StrongBox IC: fall through to the hardware/software Keystore.
        }

        val generator = KeyPairGenerator.getInstance(KEY_ALGORITHM, KEYSTORE)
        generator.initialize(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                .setKeySize(256)
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(false)
                .build(),
        )
        return generator.generateKeyPair()
    }

    class KeyStoreException(message: String) : Exception(message)

    companion object {
        const val ALIAS = "com.wattracker.android.device-signing-key"

        private const val KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALGORITHM = "EC"
    }
}
