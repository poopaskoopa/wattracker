package com.wattracker.android.cloud

import java.security.UnrecoverableKeyException
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
    /** [plaintext] as an envelope: an IV the cipher chose, then the ciphertext and its GCM tag. */
    fun encrypt(plaintext: ByteArray): ByteArray

    /**
     * The plaintext of an envelope [encrypt] wrote.
     *
     * Throws [CredentialCipherException] for anything that is not exactly the
     * bytes that were written -- another key, a flipped bit, an envelope meant
     * for another purpose, a truncated read. GCM authenticates, and this seam
     * does not turn that signal into a plausible-looking wrong answer.
     *
     * A [CredentialKeyStore.CredentialKeyException] is not wrapped: "there is no
     * key to ask" and "the vault answers badly" are different failures with
     * different advice, and a caller has to be able to tell them apart.
     */
    fun decrypt(envelope: ByteArray): ByteArray
}

/** An envelope that cannot be opened, or a cipher that could not be initialised. */
class CredentialCipherException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * AES-256-GCM, envelope = `[IV][ciphertext][tag]`.
 *
 * One layout, and one code path for it, because the IV is the one thing a
 * caller can get wrong. GCM's fatal rule is that a (key, IV) pair is never
 * reused, so the **provider** always draws the IV -- `init(ENCRYPT_MODE, key)`
 * with no [GCMParameterSpec]. That is the only mode an AndroidKeyStore AES-GCM
 * key accepts (it refuses an IV supplied by the caller), and a software key
 * draws a random one the same way, so production and the JVM tests agree.
 *
 * The IV is *not* part of what `doFinal` returns: it is read back from
 * `cipher.getIV()` and written in front by hand. Relying on the provider to
 * prefix it -- the earlier version of this class -- wrote an envelope with no
 * IV at all and decrypted without one, which throws on every real provider.
 * Decryption therefore always supplies [GCMParameterSpec] over the leading
 * [GCM_IV_BYTES], whichever side drew it.
 *
 * [associatedData] is not secret and is not stored: it is the vault's own
 * purpose, authenticated along with the payload. It separates this file from
 * any other use of the same key, so an envelope meant for a different purpose
 * refuses to open. It does **not** bind a vault to an installation -- it is one
 * constant shared by every install -- that binding is the per-installation
 * Keystore key, which never leaves the device.
 */
class AesGcmCipher(
    private val keyProvider: () -> SecretKey,
    private val associatedData: ByteArray,
) : CredentialCipher {

    override fun encrypt(plaintext: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        try {
            cipher.init(Cipher.ENCRYPT_MODE, keyProvider())
            cipher.updateAAD(associatedData)
        } catch (e: Exception) {
            throw wrapFailure("could not initialise the credential cipher", e)
        }

        // The provider chose it; the envelope has to carry it.
        val iv = cipher.iv
        if (iv == null || iv.size != GCM_IV_BYTES) {
            throw CredentialCipherException("the cipher did not produce a $GCM_IV_BYTES-byte IV")
        }

        val body = try {
            cipher.doFinal(plaintext)
        } catch (e: Exception) {
            throw wrapFailure("could not encrypt the credential vault", e)
        }
        return iv + body
    }

    override fun decrypt(envelope: ByteArray): ByteArray {
        if (envelope.size < MIN_ENVELOPE_BYTES) {
            throw CredentialCipherException("the credential envelope is shorter than an IV and a tag")
        }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        try {
            val iv = envelope.copyOfRange(0, GCM_IV_BYTES)
            cipher.init(Cipher.DECRYPT_MODE, keyProvider(), GCMParameterSpec(GCM_TAG_BITS, iv))
            cipher.updateAAD(associatedData)
            return cipher.doFinal(envelope, GCM_IV_BYTES, envelope.size - GCM_IV_BYTES)
        } catch (e: Exception) {
            throw wrapFailure("the credential vault is not one this key wrote", e)
        }
    }

    /**
     * A key problem stays a key problem. [keyProvider] throws
     * [CredentialKeyStore.CredentialKeyException] when the Keystore will not
     * hand over the key, and a stale Keystore handle says so at `init` with
     * [UnrecoverableKeyException]; wrapping either into a cipher failure would
     * tell the store "the vault is bad" when the truth is "there is no key", and
     * the store would then advise pairing again on a device that cannot write
     * one. Everything else is the envelope's own fault.
     */
    private fun wrapFailure(message: String, failure: Exception): Exception = when (failure) {
        is CredentialKeyStore.CredentialKeyException -> failure
        is UnrecoverableKeyException -> CredentialKeyStore.CredentialKeyException(
            "the credential key could not be used",
            failure,
        )
        else -> CredentialCipherException(message, failure)
    }

    companion object {
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_IV_BYTES = 12
        private const val GCM_TAG_BITS = 128

        /** An IV and a tag and nothing else is already impossible payload. */
        private const val MIN_ENVELOPE_BYTES = GCM_IV_BYTES + GCM_TAG_BITS / 8
    }
}
