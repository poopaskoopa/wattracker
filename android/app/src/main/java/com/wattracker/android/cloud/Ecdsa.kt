package com.wattracker.android.cloud

import java.math.BigInteger
import java.security.interfaces.ECPublicKey
import java.security.spec.ECPoint

/**
 * The conversion between Android's DER ECDSA signatures and the raw `r || s`
 * form the server accepts, plus the P-256 arithmetic to validate a component.
 *
 * **The trap this file exists for.** `Signature.getInstance("SHA256withECDSA")`
 * and `KeyPair.private.sign(bytes)` on Android both return a *DER*-encoded
 * `SEQUENCE { INTEGER r, INTEGER s }`. The server accepts raw `r || s` (128
 * lowercase hex) and nothing else -- a client that sends the DER bytes gets a
 * 401 with no diagnostic. So the DER is decoded here to 64 raw bytes before it
 * is hex-encoded.
 *
 * **No low-s normalization.** The server accepts a malleable signature on
 * purpose (`security.py:verify_signature`): the secure key emits a high-s
 * signature about half the time, and normalizing `s` would mean the client is
 * sending something other than what it signed. The only out-of-range check is
 * the one the server enforces too -- each component in `[1, n-1]` -- and it
 * rejects a zero or oversized component instead of emitting it.
 */
object Ecdsa {

    /** The secp256r1 (P-256) group order `n`. */
    val P256_ORDER: BigInteger = BigInteger(
        "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16,
    )

    const val FIELD_SIZE = 32

    class EcdsaException(message: String) : Exception(message)

    /**
     * Decode a DER ECDSA signature into raw `r || s` (64 bytes).
     *
     * Both components are validated to `[1, n-1]` and zero-padded to 32 bytes
     * each. Anything else -- a non-SEQUENCE, a truncated integer, a zero or
     * out-of-range component, trailing bytes -- throws [EcdsaException]. A
     * signature this refuses would be refused by the server too, so failing
     * here rather than sending is correct.
     */
    fun derToRawRorS(der: ByteArray): ByteArray {
        var pos = 0
        fun fail(message: String): Nothing = throw EcdsaException(message)
        fun byteAt(): Int {
            if (pos >= der.size) fail("truncated DER signature")
            return der[pos].toInt() and 0xFF
        }

        if (byteAt() != 0x30) fail("expected SEQUENCE tag 0x30")
        pos++
        val seqLen = byteAt()
        pos++
        if (seqLen and 0x80 != 0) fail("long-form SEQUENCE length")
        if (2 + seqLen != der.size) fail("SEQUENCE length does not match the buffer")

        fun readInteger(): BigInteger {
            if (byteAt() != 0x02) fail("expected INTEGER tag 0x02")
            pos++
            val intLen = byteAt()
            pos++
            if (intLen and 0x80 != 0) fail("long-form INTEGER length")
            if (pos + intLen > der.size) fail("INTEGER runs past the buffer")
            val bytes = der.copyOfRange(pos, pos + intLen)
            pos += intLen
            return BigInteger(bytes)
        }

        val r = readInteger()
        val s = readInteger()
        if (pos != der.size) fail("trailing bytes after the signature")
        return rawFromComponents(r, s)
    }

    /** Encode raw `r || s` (64 bytes) as a DER ECDSA signature. */
    fun rawToDer(rOrS: ByteArray): ByteArray {
        if (rOrS.size != 2 * FIELD_SIZE) {
            throw EcdsaException("raw r||s must be ${2 * FIELD_SIZE} bytes")
        }
        val r = BigInteger(1, rOrS.copyOfRange(0, FIELD_SIZE))
        val s = BigInteger(1, rOrS.copyOfRange(FIELD_SIZE, 2 * FIELD_SIZE))
        val body = derInteger(r) + derInteger(s)
        if (body.size and 0x80 != 0) throw EcdsaException("DER body too long")
        return byteArrayOf(0x30, body.size.toByte()) + body
    }

    private fun rawFromComponents(r: BigInteger, s: BigInteger): ByteArray {
        if (r.signum() <= 0 || r >= P256_ORDER) throw EcdsaException("r outside [1, n-1]")
        if (s.signum() <= 0 || s >= P256_ORDER) throw EcdsaException("s outside [1, n-1]")
        return toFixed32(r) + toFixed32(s)
    }

    private fun derInteger(value: BigInteger): ByteArray {
        // BigInteger's two's-complement encoding is already the minimal DER
        // integer body for a positive value (it keeps the leading 0x00 when the
        // high bit is set), so it needs no adjustment.
        val raw = value.toByteArray()
        if (raw.size and 0x80 != 0) throw EcdsaException("DER integer too long")
        return byteArrayOf(0x02, raw.size.toByte()) + raw
    }

    /** A non-negative [BigInteger] as exactly 32 big-endian, zero-padded bytes. */
    private fun toFixed32(value: BigInteger): ByteArray {
        val raw = value.toByteArray()
        var start = 0
        while (start < raw.size - 1 && raw[start].toInt() == 0) start++
        val significant = raw.copyOfRange(start, raw.size)
        if (significant.size > FIELD_SIZE) throw EcdsaException("component does not fit 32 bytes")
        val out = ByteArray(FIELD_SIZE)
        System.arraycopy(significant, 0, out, FIELD_SIZE - significant.size, significant.size)
        return out
    }

    /**
     * Uncompressed SEC1 / X9.63 public key: `0x04 || X (32) || Y (32)`, 65
     * bytes. The only encoding `validate_public_key` accepts.
     */
    fun x963FromPublic(publicKey: ECPublicKey): ByteArray {
        val point: ECPoint = publicKey.w
        return byteArrayOf(0x04) + toFixed32(point.affineX) + toFixed32(point.affineY)
    }
}
