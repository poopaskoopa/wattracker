package com.wattracker.android.cloud

import java.security.InvalidKeyException
import java.security.ProviderException
import java.security.UnrecoverableKeyException
import javax.crypto.AEADBadTagException
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
 *
 * The failure taxonomy is three-way on purpose, because the store has to give
 * three different answers and must not collapse them:
 *
 * - [CredentialCipherException] -- **the envelope's own fault**. GCM
 *   authenticated it and it is not a vault this key wrote, or it is too short to
 *   be an envelope at all. This is evidence about the file.
 * - [CredentialKeyStore.CredentialKeyException] -- **the key could not be
 *   used**. Nothing about the file is established, and "pair again replaces it"
 *   would be wrong advice on a device that cannot write a pairing either.
 * - [CredentialCipherUnavailableException] -- **the cipher could not be
 *   operated**. A provider hiccup or a transient `init`/`doFinal` failure. This
 *   says nothing about the envelope and must be treated like a failed read, not
 *   like a bad vault.
 */
interface CredentialCipher {
    /** [plaintext] as an envelope: an IV this cipher chose, then the ciphertext and its GCM tag. */
    fun encrypt(plaintext: ByteArray): ByteArray

    /**
     * The plaintext of an envelope [encrypt] wrote.
     *
     * Throws [CredentialCipherException] for anything that is not exactly the
     * bytes that were written -- another key, a flipped bit, an envelope meant
     * for another purpose, a truncated read. GCM authenticates, and this seam
     * does not turn that signal into a plausible-looking wrong answer.
     *
     * Throws [CredentialKeyStore.CredentialKeyException] when the key cannot be
     * used, and [CredentialCipherUnavailableException] when the cipher itself
     * could not be operated. A caller has to be able to tell all three apart.
     */
    fun decrypt(envelope: ByteArray): ByteArray
}

/** The envelope is not one this key wrote, or is not an envelope. Evidence about the file. */
class CredentialCipherException(message: String, cause: Throwable? = null) : Exception(message, cause)

/** The cipher could not be operated; nothing about the envelope is established. */
class CredentialCipherUnavailableException(message: String, cause: Throwable? = null) : Exception(message, cause)

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
        val cipher = newCipher()
        val key = keyProvider()

        try {
            cipher.init(Cipher.ENCRYPT_MODE, key)
            cipher.updateAAD(associatedData)
        } catch (e: Exception) {
            throw initFailure(e)
        }

        // The provider chose it; the envelope has to carry it. A provider that
        // does not report a 12-byte IV is unusable, not evidence about any vault.
        val iv = cipher.iv
        if (iv == null || iv.size != GCM_IV_BYTES) {
            throw CredentialCipherUnavailableException("the cipher did not produce a $GCM_IV_BYTES-byte IV")
        }

        val body = try {
            cipher.doFinal(plaintext)
        } catch (e: Exception) {
            throw operationFailure("could not encrypt the credential vault", e)
        }
        return iv + body
    }

    override fun decrypt(envelope: ByteArray): ByteArray {
        if (envelope.size < MIN_ENVELOPE_BYTES) {
            throw CredentialCipherException("the credential envelope is shorter than an IV and a tag")
        }
        val cipher = newCipher()
        val key = keyProvider()

        try {
            val iv = envelope.copyOfRange(0, GCM_IV_BYTES)
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(GCM_TAG_BITS, iv))
            cipher.updateAAD(associatedData)
        } catch (e: Exception) {
            throw initFailure(e)
        }

        return try {
            cipher.doFinal(envelope, GCM_IV_BYTES, envelope.size - GCM_IV_BYTES)
        } catch (e: AEADBadTagException) {
            // The one authenticated signal: these bytes are not a vault this key
            // wrote. This is the only decrypt failure that is evidence about the
            // file.
            throw CredentialCipherException("the credential vault is not one this key wrote", e)
        } catch (e: Exception) {
            throw operationFailure("the credential vault could not be decrypted", e)
        }
    }

    /** `Cipher.getInstance` failing is a platform problem, not a vault problem. */
    private fun newCipher(): Cipher = try {
        Cipher.getInstance(TRANSFORMATION)
    } catch (e: Exception) {
        throw CredentialCipherUnavailableException("the credential cipher is unavailable", e)
    }

    /**
     * What a failure at `init` means.
     *
     * The #459 review caught the earlier version classifying only
     * [UnrecoverableKeyException] here. `init` never throws that -- it comes from
     * `KeyStore.getKey` -- and a key the Keystore cannot use arrives as
     * [InvalidKeyException] or [ProviderException]. Left as a cipher failure, the
     * store read it as "the vault won't open", told the rider pairing again
     * would fix it, and let a save replace the file: the M1 data loss again.
     * Those are key failures. Anything else at `init` (a provider hiccup, an
     * unusable algorithm) establishes nothing about the envelope and is treated
     * as a failed operation.
     */
    private fun initFailure(failure: Exception): Exception = when (failure) {
        is CredentialKeyStore.CredentialKeyException -> failure
        is UnrecoverableKeyException,
        is InvalidKeyException,
        is ProviderException -> keyFailure(failure)
        else -> CredentialCipherUnavailableException("could not initialise the credential cipher", failure)
    }

    /** A key problem stays a key problem, wherever it surfaces. */
    private fun operationFailure(message: String, failure: Exception): Exception = when (failure) {
        is CredentialKeyStore.CredentialKeyException -> failure
        is InvalidKeyException,
        is ProviderException -> keyFailure(failure)
        else -> CredentialCipherUnavailableException(message, failure)
    }

    private fun keyFailure(failure: Exception): Exception =
        CredentialKeyStore.CredentialKeyException("the credential key could not be used", failure)

    companion object {
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_IV_BYTES = 12
        private const val GCM_TAG_BITS = 128

        /** An IV and a tag and nothing else is already impossible payload. */
        private const val MIN_ENVELOPE_BYTES = GCM_IV_BYTES + GCM_TAG_BITS / 8
    }
}
