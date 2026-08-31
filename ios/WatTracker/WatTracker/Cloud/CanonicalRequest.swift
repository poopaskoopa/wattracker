import Foundation
import CryptoKit

/// The Swift half of `wattracker/cloud/security.py:canonical_request`.
///
/// This type is the reason the walking skeleton exists.  The server signs and
/// verifies over a length-framed, domain-separated byte string, and a client
/// that produces those bytes even one byte differently gets a 401 with no
/// diagnostic on either side.  So the rules are restated here in full, and
/// `tests/vectors/canonical_request_v1.json` -- the same file the Python suite
/// asserts against -- proves the two agree rather than leaving it to review.
///
/// Three things are easy to get wrong in Swift specifically, and all three are
/// covered by a vector:
///
/// 1. **Lengths are UTF-8 byte counts.** `String.count` is grapheme clusters
///    and `utf16.count` is code units; either produces a different prefix for
///    a non-ASCII field and a signature that verifies nowhere.
/// 2. **An empty field is still a field.** The refresh envelope's revision is
///    the empty string, which contributes a four-byte zero length and no
///    bytes.  Skipping it is the single most likely way to break refresh.
/// 3. **The method is upper-cased before framing**, not after.
enum CanonicalRequest {
    /// `wattracker-cloud-request-v1\0`, matching `_CANONICAL_DOMAIN`.
    static let domainSeparator: Data = {
        var data = Data("wattracker-cloud-request-v1".utf8)
        data.append(0x00)
        return data
    }()

    enum Failure: Error, CustomStringConvertible {
        case invalidField(String, String)

        var description: String {
            switch self {
            case let .invalidField(name, reason): return "\(name) \(reason)"
            }
        }
    }

    /// Serialize the request fields exactly as the server will re-serialize
    /// them when it verifies the signature.
    ///
    /// The validation here mirrors the server's, and deliberately fails rather
    /// than sanitising: a field this rejects would be rejected by the server
    /// too, and failing at the point of construction produces an error that
    /// names the field instead of an unexplained 401 later.
    static func bytes(
        method: String,
        path: String,
        namespace: String,
        timestamp: String,
        nonce: String,
        bodyDigest: String,
        idempotencyKey: String,
        revision: String
    ) throws -> Data {
        let normalizedMethod = method.uppercased()
        try requireMatch(normalizedMethod, pattern: "^[A-Z][A-Z0-9_\\-]{0,31}$", field: "method")
        try requireText(path, field: "path")
        guard path.hasPrefix("/") else {
            throw Failure.invalidField("path", "must begin with /")
        }
        try requireMatch(namespace, pattern: "^[0-9a-f]{64}$", field: "namespace")
        try requireText(timestamp, field: "timestamp")
        try requireText(nonce, field: "nonce")
        guard nonce.utf8.count <= 512 else {
            throw Failure.invalidField("nonce", "is longer than 512 bytes")
        }
        try requireMatch(bodyDigest, pattern: "^[0-9a-f]{64}$", field: "body_digest")
        try requireText(idempotencyKey, field: "idempotency_key")
        guard idempotencyKey.utf8.count <= 256 else {
            throw Failure.invalidField("idempotency_key", "is longer than 256 bytes")
        }
        // The revision alone may be empty; it still contributes a length.
        try requireNoControlCharacters(revision, field: "revision")

        let fields = [
            normalizedMethod, path, namespace, timestamp,
            nonce, bodyDigest, idempotencyKey, revision,
        ]
        var output = domainSeparator
        for field in fields {
            let encoded = Data(field.utf8)
            var length = UInt32(encoded.count).bigEndian
            withUnsafeBytes(of: &length) { output.append(contentsOf: $0) }
            output.append(encoded)
        }
        return output
    }

    /// Lowercase hex SHA-256 of a request body, matching `digest_body`.
    static func digestBody(_ body: Data) -> String {
        SHA256.hash(data: body).map { String(format: "%02x", $0) }.joined()
    }

    // MARK: - Field validation

    private static func requireNoControlCharacters(_ value: String, field: String) throws {
        // The server refuses NUL, CR and LF in every textual protocol field so
        // that a signed representation cannot be re-read differently by an
        // HTTP layer.  Refusing them here keeps the two in step.
        if value.utf8.contains(where: { $0 == 0x00 || $0 == 0x0D || $0 == 0x0A }) {
            throw Failure.invalidField(field, "contains a control character")
        }
    }

    private static func requireText(_ value: String, field: String) throws {
        guard !value.isEmpty else {
            throw Failure.invalidField(field, "must not be empty")
        }
        try requireNoControlCharacters(value, field: field)
    }

    private static func requireMatch(_ value: String, pattern: String, field: String) throws {
        try requireText(value, field: field)
        guard value.range(of: pattern, options: .regularExpression) != nil else {
            throw Failure.invalidField(field, "is invalid")
        }
    }
}
