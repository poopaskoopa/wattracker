package com.wattracker.android.cloud

import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The credential store's envelope seam: bytes in, bytes out, and a decryption
 * that **refuses** rather than returning garbage.
 *
 * The interface exists so the store's rules -- what a corrupt file means, what
 * a refused write means, that nothing is ever left in the clear -- are testable
 * on the JVM with no Android in it, exactly like [DeviceCredentialStore]
 * itself. Production passes [AesGcmCipher] a key from [CredentialKeyStore],
 * which this process cannot read; the tests hand it the same cipher with a
 * software key.
 */
interface CredentialCipher {
    /** [plaintext] as an envelope: an IV this cipher chose, then the ciphertext and its GCM tag. */
    fun encrypt(plaintext: ByteArray): ByteArray

    /**
     * The plaintext of an envelope [encrypt] wrote.
     *
     * Throws [CredentialCipherException] for anything that is not exactly the
     * bytes that were written -- another key, a flipped bit, an envelope meant
     * for another file, a truncated read. GCM authenticates, and this seam does
     * not turn that signal into a plausible-looking wrong answer.
     */
    fun decrypt(envelope: ByteArray): ByteArray
}

/** An envelope that cannot be opened, or a cipher that could not be initialised. */
class CredentialCipherException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * AES-256-GCM, envelope = `[IV][ciphertext][tag]`.
 *
 * GCM's one fatal rule is that a (key, IV) pair is never reused, and [ivSource]
 * is how the two callers obey it without the other having to know: with a
 * software key this class draws a fresh 12-byte IV per encrypt and writes it in
 * front; with an AndroidKeyStore key the **provider** draws it -- the Keystore
 * refuses an IV it was not asked for and prefixes its own 12 bytes to the
 * ciphertext, which is the same envelope this class produces by hand. One
 * layout, whichever side chose the IV.
 *
 * [associatedData] is not secret and is not stored: it is the file's own name
 * and purpose, authenticated along with the payload. It is what makes a
 * credential file *carried* from somewhere else -- Auto Backup, a pull off a
 * rooted device, one app's file renamed over another's -- refuse to open even
 * on the device that wrote it, because the tag no longer covers what it covers.
 */
class AesGcmCipher(
    private val keyProvider: () -> SecretKey,
    private val associatedData: ByteArray,
    private val ivSource: IvSource = IvSource.LOCAL,
) : CredentialCipher {

    /** Who chooses the GCM IV. Both produce the same envelope. */
    enum class IvSource {
        /** A software key: this class draws a fresh IV and prefixes it. */
        LOCAL,

        /** An AndroidKeyStore key: the provider draws it and prefixes it. */
        PROVIDER_GENERATED,
    }

    override fun encrypt(plaintext: ByteArray): ByteArray =
        if (ivSource == IvSource.LOCAL) encryptWithLocalIv(plaintext) else encryptWithProviderIv(plaintext)

    override fun decrypt(envelope: ByteArray): ByteArray {
        if (envelope.size < MIN_ENVELOPE_BYTES) {
            throw CredentialCipherException("the credential envelope is shorter than an IV and a tag")
        }
        return if (ivSource == IvSource.LOCAL) decryptWithLocalIv(envelope) else decryptWithProviderIv(envelope)
    }

    private fun encryptWithLocalIv(plaintext: ByteArray): ByteArray {
        val iv = ByteArray(GCM_IV_BYTES).also { SecureRandom().nextBytes(it) }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        try {
            cipher.init(Cipher.ENCRYPT_MODE, keyProvider(), GCMParameterSpec(GCM_TAG_BITS, iv))
            cipher.updateAAD(associatedData)
            return iv + cipher.doFinal(plaintext)
        } catch (e: Exception) {
            throw CredentialCipherException("could not encrypt the credential vault", e)
        }
    }

    private fun decryptWithLocalIv(envelope: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        try {
            val iv = envelope.copyOfRange(0, GCM_IV_BYTES)
            cipher.init(Cipher.DECRYPT_MODE, keyProvider(), GCMParameterSpec(GCM_TAG_BITS, iv))
            cipher.updateAAD(associatedData)
            return cipher.doFinal(envelope, GCM_IV_BYTES, envelope.size - GCM_IV_BYTES)
        } catch (e: Exception) {
            throw CredentialCipherException("the credential vault is not one this key wrote", e)
        }
    }

    /**
     * The AndroidKeyStore half. No [GCMParameterSpec]: a Keystore AES-GCM key
     * rejects an IV supplied by the caller, and the provider writes its own in
     * front of the ciphertext, so the envelope needs no extra work here --
     * which is the whole reason [IvSource] exists rather than two formats.
     */
    private fun encryptWithProviderIv(plaintext: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        try {
            cipher.init(Cipher.ENCRYPT_MODE, keyProvider())
            cipher.updateAAD(associatedData)
            return cipher.doFinal(plaintext)
        } catch (e: Exception) {
            throw CredentialCipherException("could not encrypt the credential vault", e)
        }
    }

    private fun decryptWithProviderIv(envelope: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        try {
            cipher.init(Cipher.DECRYPT_MODE, keyProvider())
            cipher.updateAAD(associatedData)
            return cipher.doFinal(envelope)
        } catch (e: Exception) {
            throw CredentialCipherException("the credential vault is not one this key wrote", e)
        }
    }

    companion object {
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_IV_BYTES = 12
        private const val GCM_TAG_BITS = 128

        /** An IV and a tag and nothing else is already impossible payload. */
        private const val MIN_ENVELOPE_BYTES = GCM_IV_BYTES + GCM_TAG_BITS / 8
    }
}
