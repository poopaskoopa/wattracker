package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.opt
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optString
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.math.BigInteger
import java.security.KeyFactory
import java.security.PublicKey
import java.security.Signature
import java.security.spec.X509EncodedKeySpec

/**
 * Tests for the DER-to-raw signature conversion and the shared P-256 signature
 * vectors in `tests/vectors/canonical_request_v1.json`.
 *
 * The DER trap: Android's EC signing returns DER, the server wants raw `r ||
 * s`. If the conversion is wrong, or if it silently normalises `s` to low-s,
 * half of all refreshes fail in production with a 401. The vectors cover both.
 */
class EcdsaTest {

    private val vectors: JsonValue = TestVectors.parse("canonical_request_v1.json")

    /** The fixed P-256 SubjectPublicKeyInfo header, before the 65-byte point. */
    private val spkiPrefixHex = "3059301306072a8648ce3d020106082a8648ce3d030107034200"

    private fun publicKeyFromX963Hex(hex: String): PublicKey {
        val point = hex.hexToBytes() ?: throw AssertionError("bad public key hex")
        assertEquals(65, point.size)
        assertEquals(0x04, point[0].toInt() and 0xFF)
        val spki = spkiPrefixHex.hexToBytes()!!.plus(point)
        return KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(spki))
    }

    private fun verify(pub: PublicKey, message: ByteArray, rawSig: ByteArray): Boolean =
        try {
            val sig = Signature.getInstance("SHA256withECDSA")
            sig.initVerify(pub)
            sig.update(message)
            sig.verify(Ecdsa.rawToDer(rawSig))
        } catch (e: Exception) {
            false
        }

    private fun signatureVectors(): JsonValue =
        vectors.opt("signature_vectors") as? JsonValue.Object
            ?: throw AssertionError("missing signature_vectors")

    private fun canonicalFor(vectorName: String): ByteArray {
        val cases = (vectors.optArray("canonical_requests") as? List<JsonValue>)!!
        val entry = cases.first { it.optString("name") == vectorName }
        val timestamp = when (val ts = entry.opt("timestamp")) {
            is JsonValue.Number -> ts.value.toLong().toString()
            is JsonValue.String -> ts.value
            else -> throw AssertionError("bad timestamp")
        }
        return CanonicalRequest.bytes(
            method = entry.optString("method")!!,
            path = entry.optString("path")!!,
            namespace = entry.optString("namespace")!!,
            timestamp = timestamp,
            nonce = entry.optString("nonce")!!,
            bodyDigest = entry.optString("body_digest")!!,
            idempotencyKey = entry.optString("idempotency_key")!!,
            revision = entry.optString("revision")!!,
        )
    }

    @Test
    fun theSharedSignatureVectorsVerify() {
        val sig = signatureVectors()
        assertEquals("ecdsa-p256-sha256", sig.optString("algorithm"))
        val publicKey = publicKeyFromX963Hex(sig.optString("public_key_x963_hex")!!)
        val canonical = canonicalFor(sig.optString("canonical_vector")!!)

        for (entry in (sig.optArray("must_verify") as List<JsonValue>)) {
            val raw = entry.optString("signature_hex")!!.hexToBytes()!!
            val why = entry.optString("why") ?: ""
            assertTrue("${entry.optString("name")} must verify: $why", verify(publicKey, canonical, raw))
        }
        for (entry in (sig.optArray("must_not_verify") as List<JsonValue>)) {
            val raw = entry.optString("signature_hex")!!.hexToBytes()!!
            assertFalse("${entry.optString("name")} must not verify", verify(publicKey, canonical, raw))
        }
    }

    @Test
    fun bothSValuesSurviveDerRoundTripUnmodified() {
        // No low-s normalisation: the raw form -- including the high-s twin --
        // must come back through DER byte-for-byte identical.
        val sig = signatureVectors()
        for (entry in (sig.optArray("must_verify") as List<JsonValue>)) {
            val raw = entry.optString("signature_hex")!!.hexToBytes()!!
            val round = Ecdsa.derToRawRorS(Ecdsa.rawToDer(raw))
            assertArrayEquals(entry.optString("name"), raw, round)
        }
    }

    @Test
    fun theHighSTwinIsTheMalleableTwinOfTheLowSOne() {
        val sig = signatureVectors()
        val byName = (sig.optArray("must_verify") as List<JsonValue>).associateBy { it.optString("name") }
        val lowS = BigInteger(1, byName["low-s"]!!.optString("signature_hex")!!.hexToBytes()!!.copyOfRange(32, 64))
        val highS = BigInteger(1, byName["high-s"]!!.optString("signature_hex")!!.hexToBytes()!!.copyOfRange(32, 64))
        assertEquals(Ecdsa.P256_ORDER, lowS.add(highS))
        assertTrue(lowS < Ecdsa.P256_ORDER.shiftRight(1))
        assertTrue(highS > Ecdsa.P256_ORDER.shiftRight(1))
    }

    @Test
    fun aLeadingSignByteInDerIsStripped() {
        // An r whose high bit is set needs a leading 0x00 in the DER integer;
        // the raw form drops it and left-pads to 32 bytes.
        val raw = ByteArray(64)
        raw[0] = 0x80.toByte() // high bit set -> DER adds a sign byte
        raw[32] = 0x81.toByte()
        val der = Ecdsa.rawToDer(raw)
        // The r INTEGER is 0x02, length 0x21 (33), 0x00, then the 32 bytes.
        assertTrue("DER should carry a 33-byte r integer", der.contains(subseq(0x02, 0x21, 0x00)))
        assertArrayEquals(raw, Ecdsa.derToRawRorS(der))
    }

    @Test
    fun aShortIntegerIsZeroPaddedOnTheLeft() {
        // r = 1 and s = 1: each encodes as a one-byte DER integer, and the
        // raw form must come back as 32 bytes zero-padded on the left.
        val raw = ByteArray(64)
        raw[31] = 0x01 // r = 1
        raw[63] = 0x01 // s = 1
        val der = Ecdsa.rawToDer(raw)
        // Both integers are the short form `02 01 01`.
        assertTrue(der.contains(subseq(0x02, 0x01, 0x01)))
        assertArrayEquals(raw, Ecdsa.derToRawRorS(der))
    }

    @Test
    fun aZeroComponentIsRejected() {
        val raw = ByteArray(64) // r = 0, s = 0
        val der = Ecdsa.rawToDer(raw)
        try {
            Ecdsa.derToRawRorS(der)
            throw AssertionError("a zero component must be rejected")
        } catch (e: Ecdsa.EcdsaException) {
            // expected
        }
    }

    @Test
    fun aComponentAtTheOrderIsRejected() {
        // r = n is outside [1, n-1] and must be refused before emission. The
        // 32-byte form of n is its hex without the leading sign byte that
        // BigInteger.toByteArray() would add.
        val n = "ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551"
            .hexToBytes()!!
        val raw = n + ByteArray(31) + byteArrayOf(0x01) // r = n (32B), s = 1 (32B)
        val der = Ecdsa.rawToDer(raw)
        try {
            Ecdsa.derToRawRorS(der)
            throw AssertionError("r = n must be rejected")
        } catch (e: Ecdsa.EcdsaException) {
            // expected
        }
    }

    @Test
    fun aNonSequenceDerIsRejected() {
        try {
            Ecdsa.derToRawRorS(byteArrayOf(0x01, 0x02, 0x03))
            throw AssertionError("a non-SEQUENCE must be rejected")
        } catch (e: Ecdsa.EcdsaException) {
            // expected
        }
    }

    private fun subseq(vararg bytes: Int): List<Byte> = bytes.map { it.toByte() }

    private fun ByteArray.contains(sub: List<Byte>): Boolean {
        outer@ for (i in 0..size - sub.size) {
            for (j in sub.indices) {
                if (this[i + j] != sub[j]) continue@outer
            }
            return true
        }
        return false
    }
}
