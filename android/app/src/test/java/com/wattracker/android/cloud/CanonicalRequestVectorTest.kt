package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.asString
import com.wattracker.android.json.opt
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optInt
import com.wattracker.android.json.optString
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest
import java.util.Base64

/**
 * The Kotlin half of the shared canonical-request interop vectors.
 *
 * This reads `tests/vectors/canonical_request_v1.json` -- the same file the
 * Python suite (`tests/test_canonical_request_vectors.py`) and the Swift suite
 * (`CanonicalRequestVectorTests`) read -- referenced from the repository, not
 * copied. If this fails and the other two pass, the Android client would sign
 * something the server will not verify, and the only symptom in production
 * would be a 401 with an empty body.
 */
class CanonicalRequestVectorTest {

    private val vectors: JsonValue = TestVectors.parse("canonical_request_v1.json")

    private fun cases(key: String): List<JsonValue> =
        vectors.optArray(key) ?: throw AssertionError("vector file is missing $key")

    // MARK: - Framing

    @Test
    fun domainSeparatorMatchesTheServer() {
        val encoded = vectors.optString("domain_separator_base64")
            ?: throw AssertionError("missing domain_separator_base64")
        assertArrayEquals(Base64.getDecoder().decode(encoded), CanonicalRequest.DOMAIN_SEPARATOR)
    }

    @Test
    fun fieldOrderMatchesTheServer() {
        assertEquals(
            listOf(
                "method", "path", "namespace", "timestamp",
                "nonce", "body_digest", "idempotency_key", "revision",
            ),
            vectors.optArray("field_order")?.map { it.asString() },
        )
    }

    // MARK: - Body digests

    @Test
    fun everyBodyDigestVectorMatches() {
        for (entry in cases("body_digests")) {
            val name = entry.optString("name")!!
            val body = base64(entry.optString("body_base64")!!)
            assertEquals(name, entry.optString("digest"), CanonicalRequest.digestBody(body))
        }
    }

    // MARK: - Canonical requests

    @Test
    fun everyCanonicalRequestVectorMatchesByteForByte() {
        val all = cases("canonical_requests")
        assertTrue("the vector file carries no cases", all.isNotEmpty())
        for (entry in all) {
            val name = entry.optString("name")!!
            val expected = base64(entry.optString("canonical_base64")!!)
            val produced = canonical(entry)
            assertArrayEquals(name, expected, produced)
            assertEquals(name, entry.optInt("canonical_length")!!, produced.size)
            assertEquals(name, entry.optString("canonical_sha256")!!, sha256Hex(produced))
        }
    }

    @Test
    fun theRecordedBodyDigestIsTheDigestOfTheRecordedBody() {
        // Otherwise a client could reproduce every canonical vector while
        // hashing bodies wrongly, and only fail against a live server.
        for (entry in cases("canonical_requests")) {
            val name = entry.optString("name")!!
            val body = base64(entry.optString("body_base64")!!)
            assertEquals(name, entry.optString("body_digest")!!, CanonicalRequest.digestBody(body))
        }
    }

    @Test
    fun boundaryPairsAreDistinct() {
        val pairs = vectors.optArray("distinct_pairs")
            ?: throw AssertionError("missing distinct_pairs")
        assertTrue("the framing claim needs at least one pair", pairs.isNotEmpty())
        val byName = cases("canonical_requests").associateBy { it.optString("name") }
        for (pair in pairs) {
            // Each entry is a two-element array of case names.
            val names = (pair as JsonValue.Array).values.map { it.asString()!! }
            val left = canonical(byName[names[0]]!!)
            val right = canonical(byName[names[1]]!!)
            // contentEquals: assertEquals/assertNotEquals on two ByteArrays
            // compare references, so the old form passed whatever the bytes
            // were.
            assertFalse("${names[0]} vs ${names[1]}", left.contentEquals(right))
        }
    }

    @Test
    fun unicodeFieldsAreFramedByUtf8ByteLength() {
        // A UTF-16 count would produce a shorter prefix here.
        val entry = cases("canonical_requests").first { it.optString("name") == "unicode-idempotency-key" }
        val key = entry.optString("idempotency_key")!!
        val utf8 = key.toByteArray(Charsets.UTF_8)
        assertTrue("vector lost its multibyte content", utf8.size > key.length)
        val framed = ByteArray(4 + utf8.size)
        framed[0] = ((utf8.size shr 24) and 0xFF).toByte()
        framed[1] = ((utf8.size shr 16) and 0xFF).toByte()
        framed[2] = ((utf8.size shr 8) and 0xFF).toByte()
        framed[3] = (utf8.size and 0xFF).toByte()
        System.arraycopy(utf8, 0, framed, 4, utf8.size)
        assertTrue("framed field not present", containsBytes(canonical(entry), framed))
    }

    @Test
    fun anEmptyRevisionStillContributesAZeroLength() {
        // The refresh envelope. Dropping the empty field is the single most
        // likely way for a client to break token refresh and nothing else.
        val entry = cases("canonical_requests").first { it.optString("name") == "empty-body-refresh" }
        val produced = canonical(entry)
        val tail = produced.copyOfRange(produced.size - 4, produced.size)
        assertArrayEquals("the empty revision must end in a zero-length prefix", ByteArray(4), tail)
    }

    // MARK: - Helpers

    private fun canonical(entry: JsonValue): ByteArray {
        // A timestamp is decimal text on the wire whether the vector file
        // records it as a JSON number or a JSON string.
        val timestamp = when (val ts = entry.opt("timestamp")) {
            is JsonValue.Number -> ts.value.toLong().toString()
            is JsonValue.String -> ts.value
            else -> throw AssertionError("timestamp is neither a number nor a string")
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

    private fun base64(text: String): ByteArray = Base64.getDecoder().decode(text)

    private fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).toHex()

    private fun containsBytes(haystack: ByteArray, needle: ByteArray): Boolean {
        outer@ for (i in 0..haystack.size - needle.size) {
            for (j in needle.indices) {
                if (haystack[i + j] != needle[j]) continue@outer
            }
            return true
        }
        return false
    }
}
