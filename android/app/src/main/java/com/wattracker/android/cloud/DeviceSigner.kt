package com.wattracker.android.cloud

import java.security.KeyPair
import java.security.Signature
import java.security.interfaces.ECPublicKey

/**
 * Where the device's signing key lives, and what it will sign.
 *
 * The Kotlin twin of `ios/.../Cloud/DeviceKey.swift`. The private half is a
 * P-256 key (the same reason the server carries an `ecdsa-p256-sha256`
 * algorithm at all), and the two encodings the server demands come straight
 * off this interface.
 */
interface DeviceSigner {
    /**
     * Uncompressed SEC1 / X9.63: `0x04 || X || Y`, 65 bytes. The only encoding
     * `validate_public_key` accepts.
     */
    val publicKeyX963: ByteArray

    /**
     * Raw `r || s`, 64 bytes, which the caller hex-encodes. Never DER, and
     * never normalised to low-s: the server accepts a malleable signature on
     * purpose, and rewriting `s` would mean sending something other than what
     * was signed.
     */
    fun signature(over: ByteArray): ByteArray

    /**
     * Whether the private half is in hardware. Reported, never trusted for an
     * authorization decision -- the server cannot tell the difference and
     * must not be asked to.
     */
    val isHardwareBacked: Boolean
}

/**
 * A [DeviceSigner] over a plain Java [KeyPair].
 *
 * This is what runs on both an Android Keystore key and a JVM-generated key:
 * `Signature` with `SHA256withECDSA` produces a DER `SEQUENCE{INTEGER r,
 * INTEGER s}`, which is what [Ecdsa.derToRawRorS] decodes to the raw `r || s`
 * the server wants. Hashing is done by the provider (SHA-256 for a P-256
 * signing key, which is what `ecdsa-p256-sha256` names); the input is the
 * canonical bytes, not a pre-hash.
 */
class KeyPairSigner(
    private val keyPair: KeyPair,
    private val hardwareBacked: Boolean,
) : DeviceSigner {

    override val isHardwareBacked: Boolean = hardwareBacked

    override val publicKeyX963: ByteArray by lazy {
        Ecdsa.x963FromPublic(keyPair.public as ECPublicKey)
    }

    override fun signature(over: ByteArray): ByteArray {
        val signature = Signature.getInstance("SHA256withECDSA")
        signature.initSign(keyPair.private)
        signature.update(over)
        return Ecdsa.derToRawRorS(signature.sign())
    }
}
