package com.wattracker.android.cloud

import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import javax.crypto.SecretKey
import javax.crypto.spec.SecretKeySpec

/**
 * The parts of [CredentialKeyStore] a JVM test can reach -- which is a narrower
 * set than the review would like, and the reason is the same one that already
 * keeps [DeviceKeyStore] uncovered here: `AndroidKeyStore` is not a provider a
 * unit test has.
 *
 * What that leaves is worth pinning anyway.
 *
 * - **A Keystore this platform cannot open comes back as a key failure.** Left
 *   raw, a `KeyStoreException` or `UnrecoverableKeyException` reaches the store
 *   as generic I/O, the store reads it as "the vault won't open", and the rider
 *   is told pairing again will replace a pairing this device cannot save. The
 *   JVM hits exactly that path on its very first line, because there is no
 *   `AndroidKeyStore` here to ask.
 * - **The non-exportability premise is a plain check on a [SecretKey]**, so it
 *   is testable even though the key is not. Deleting it, or letting an exportable
 *   key through, has to redden something.
 *
 * What only a device can say stays a device check: that a real `AndroidKeyStore`
 * AES-GCM key reports its provider-drawn IV as 12 bytes, that the entry survives
 * a relaunch, and that `setRandomizedEncryptionRequired(true)` is what makes the
 * provider own the IV (a caller-supplied `GCMParameterSpec` is refused in
 * encrypt mode -- that refusal is a provider behaviour, and no JVM test can
 * produce it).
 */
class CredentialKeyStoreTest {

    @Test
    fun aKeystoreThisPlatformCannotOpenIsAKeyFailureNotRawIo() {
        val loadFailure = try {
            CredentialKeyStore.loadOrCreateKey()
            null
        } catch (e: Exception) {
            e
        }
        assertNotNull("the JVM has no AndroidKeyStore provider, so this must fail", loadFailure)
        assertTrue(
            "and it must fail as a key problem: the caller chooses its fallback on that type",
            loadFailure is CredentialKeyStore.CredentialKeyException,
        )

        val deleteFailure = try {
            CredentialKeyStore.deleteKey()
            null
        } catch (e: Exception) {
            e
        }
        assertTrue(
            "the recovery path is held to the same rule",
            deleteFailure is CredentialKeyStore.CredentialKeyException,
        )
    }

    @Test
    fun anExportableKeyRefusesTheStore() {
        val thrown = try {
            CredentialKeyStore.assertNonExportable(SecretKeySpec(ByteArray(32) { it.toByte() }, "AES"))
            null
        } catch (e: Exception) {
            e
        }
        assertNotNull(
            "a key whose bytes can be read is not the premise this store is built on",
            thrown,
        )
        assertTrue(thrown is CredentialKeyStore.CredentialKeyException)
    }

    /**
     * The two shapes an unexportable Keystore key takes: `getEncoded()` returns
     * null, or it throws. Either is a pass; a store that asked for a third shape
     * would unpair devices whose provider merely reports it differently.
     */
    @Test
    fun aKeyThatWillNotHandOverItsBytesPasses() {
        CredentialKeyStore.assertNonExportable(NullEncodedKey())
        CredentialKeyStore.assertNonExportable(ThrowingEncodedKey())
    }

    /** A key that answers null for its bytes, as an AndroidKeyStore key does. */
    private class NullEncodedKey : SecretKey {
        override fun getAlgorithm(): String = "AES"
        override fun getEncoded(): ByteArray? = null
        override fun getFormat(): String = "RAW"
    }

    /** A key that refuses the question outright. */
    private class ThrowingEncodedKey : SecretKey {
        override fun getAlgorithm(): String = "AES"
        override fun getEncoded(): ByteArray = throw UnsupportedOperationException("export is not possible")
        override fun getFormat(): String? = null
    }
}
