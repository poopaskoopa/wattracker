package com.wattracker.android.cloud

import java.security.MessageDigest

/**
 * The Kotlin half of `wattracker/cloud/security.py:canonical_request`.
 *
 * This type is the reason the walking skeleton exists. The server signs and
 * verifies over a length-framed, domain-separated byte string, and a client
 * that produces those bytes even one byte differently gets a 401 with no
 * diagnostic on either side. So the rules are restated here in full, and
 * `tests/vectors/canonical_request_v1.json` -- the same file the Python and
 * Swift suites assert against -- proves the three agree rather than leaving it
 * to review.
 *
 * Three things are easy to get wrong in Kotlin specifically, and all three are
 * covered by a vector:
 *
 * 1. **Lengths are UTF-8 byte counts.** `String.length` is UTF-16 code units;
 *    it produces a different prefix for a non-ASCII field and a signature that
 *    verifies nowhere. Every length here is computed from `toByteArray()`.
 * 2. **An empty field is still a field.** The refresh envelope's revision is
 *    the empty string, which contributes a four-byte zero length and no bytes.
 *    Skipping it is the single most likely way to break refresh.
 * 3. **The method is upper-cased before framing**, not after.
 */
object CanonicalRequest {

    /** `wattracker-cloud-request-v1\0`, matching `_CANONICAL_DOMAIN`. */
    val DOMAIN_SEPARATOR: ByteArray =
        "wattracker-cloud-request-v1".toByteArray(Charsets.UTF_8)
            .plus(0.toByte())

    /** The field order the framing walks, exactly as the server serializes. */
    val FIELD_ORDER: List<String> = listOf(
        "method", "path", "namespace", "timestamp",
        "nonce", "body_digest", "idempotency_key", "revision",
    )

    class InvalidField(val field: String, val reason: String) :
        Exception("$field $reason")

    /**
     * Serialize the request fields exactly as the server will re-serialize
     * them when it verifies the signature.
     *
     * The validation mirrors the server's and deliberately fails rather than
     * sanitizing: a field this rejects would be rejected by the server too,
     * and failing at construction names the field instead of producing an
     * unexplained 401 later.
     */
    @Throws(InvalidField::class)
    fun bytes(
        method: String,
        path: String,
        namespace: String,
        timestamp: String,
        nonce: String,
        bodyDigest: String,
        idempotencyKey: String,
        revision: String,
    ): ByteArray {
        val normalizedMethod = method.uppercase()
        requireMatch(normalizedMethod, METHOD_RE, "method")
        requireText(path, "path")
        require(path.startsWith("/")) { throw InvalidField("path", "must begin with /") }
        requireMatch(namespace, NAMESPACE_RE, "namespace")
        requireText(timestamp, "timestamp")
        requireText(nonce, "nonce")
        require(nonce.toByteArray(Charsets.UTF_8).size <= 512) {
            throw InvalidField("nonce", "is longer than 512 bytes")
        }
        requireMatch(bodyDigest, DIGEST_RE, "body_digest")
        requireText(idempotencyKey, "idempotency_key")
        require(idempotencyKey.toByteArray(Charsets.UTF_8).size <= 256) {
            throw InvalidField("idempotency_key", "is longer than 256 bytes")
        }
        // The revision alone may be empty; it still contributes a length.
        requireNoControlCharacters(revision, "revision")

        val fields = listOf(
            normalizedMethod, path, namespace, timestamp,
            nonce, bodyDigest, idempotencyKey, revision,
        )
        val out = ArrayList<Byte>(DOMAIN_SEPARATOR.size + fields.sumOf {
            4 + it.toByteArray(Charsets.UTF_8).size
        })
        out.addAll(DOMAIN_SEPARATOR.toList())
        for (field in fields) {
            val encoded = field.toByteArray(Charsets.UTF_8)
            val length = encoded.size
            // 4-byte big-endian length prefix.
            out.add(((length shr 24) and 0xFF).toByte())
            out.add(((length shr 16) and 0xFF).toByte())
            out.add(((length shr 8) and 0xFF).toByte())
            out.add((length and 0xFF).toByte())
            out.addAll(encoded.toList())
        }
        return out.toByteArray()
    }

    /** Lowercase hex SHA-256 of a request body, matching `digest_body`. */
    fun digestBody(body: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(body).toHex()

    private val METHOD_RE = Regex("^[A-Z][A-Z0-9_\\-]{0,31}$")
    private val NAMESPACE_RE = Regex("^[0-9a-f]{64}$")
    private val DIGEST_RE = Regex("^[0-9a-f]{64}$")

    private fun requireNoControlCharacters(value: String, field: String) {
        // The server refuses NUL, CR and LF in every textual protocol field so
        // that a signed representation cannot be re-read differently by an
        // HTTP layer. Refusing them here keeps the two in step.
        for (c in value) {
            if (c == '\u0000' || c == '\u000D' || c == '\u000A') {
                throw InvalidField(field, "contains a control character")
            }
        }
    }

    private fun requireText(value: String, field: String) {
        if (value.isEmpty()) throw InvalidField(field, "must not be empty")
        requireNoControlCharacters(value, field)
    }

    private fun requireMatch(value: String, pattern: Regex, field: String) {
        requireText(value, field)
        if (!pattern.matches(value)) throw InvalidField(field, "is invalid")
    }
}
