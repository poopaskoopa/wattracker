package com.wattracker.android.cloud

import java.security.MessageDigest

/**
 * Constructs length-framed, domain-separated byte strings for request signing and verification,
 * matching `wattracker/cloud/security.py:canonical_request`.
 *
 * Tested against shared vectors in `tests/vectors/canonical_request_v1.json`.
 *
 * Key encoding rules:
 * 1. Lengths are UTF-8 byte counts (not UTF-16 code units).
 * 2. Empty fields contribute a 4-byte zero length prefix (`0x00000000`).
 * 3. HTTP method is upper-cased prior to framing.
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
        val encodedFields = fields.map { it.toByteArray(Charsets.UTF_8) }
        val totalSize = DOMAIN_SEPARATOR.size + encodedFields.sumOf { 4 + it.size }
        val out = ByteArray(totalSize)
        var pos = 0

        System.arraycopy(DOMAIN_SEPARATOR, 0, out, pos, DOMAIN_SEPARATOR.size)
        pos += DOMAIN_SEPARATOR.size

        for (encoded in encodedFields) {
            val len = encoded.size
            out[pos++] = ((len shr 24) and 0xFF).toByte()
            out[pos++] = ((len shr 16) and 0xFF).toByte()
            out[pos++] = ((len shr 8) and 0xFF).toByte()
            out[pos++] = (len and 0xFF).toByte()
            System.arraycopy(encoded, 0, out, pos, len)
            pos += len
        }
        return out
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
