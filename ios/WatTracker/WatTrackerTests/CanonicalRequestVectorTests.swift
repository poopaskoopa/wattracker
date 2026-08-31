import XCTest
import CryptoKit

/// The Swift half of the shared canonical-request interop vectors.
///
/// The file this reads, `tests/vectors/canonical_request_v1.json`, is the same
/// file `tests/test_canonical_request_vectors.py` reads -- referenced from the
/// repository, not copied here, so the two suites cannot drift apart. It is
/// bundled into this test target as a resource.
///
/// If this fails and the Python suite passes, the Swift client would sign
/// something the server will not verify, and the only symptom in production
/// would be a 401 with an empty body.
final class CanonicalRequestVectorTests: XCTestCase {
    private var vectors: [String: Any]!

    override func setUpWithError() throws {
        let bundle = Bundle(for: type(of: self))
        let url = try XCTUnwrap(
            bundle.url(forResource: "canonical_request_v1", withExtension: "json"),
            "The shared vector file is not in the test bundle. It is referenced "
                + "from tests/vectors/ in the repository root; check the "
                + "Resources build phase of WatTrackerTests."
        )
        let data = try Data(contentsOf: url)
        vectors = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
    }

    // MARK: - Framing

    func testDomainSeparatorMatchesTheServer() throws {
        let encoded = try XCTUnwrap(vectors["domain_separator_base64"] as? String)
        let expected = try XCTUnwrap(Data(base64Encoded: encoded))
        XCTAssertEqual(CanonicalRequest.domainSeparator, expected)
    }

    func testFieldOrderMatchesTheServer() throws {
        XCTAssertEqual(
            try XCTUnwrap(vectors["field_order"] as? [String]),
            ["method", "path", "namespace", "timestamp", "nonce",
             "body_digest", "idempotency_key", "revision"]
        )
    }

    // MARK: - Body digests

    func testEveryBodyDigestVectorMatches() throws {
        for entry in try cases(named: "body_digests") {
            let name = try XCTUnwrap(entry["name"] as? String)
            let body = try XCTUnwrap(
                Data(base64Encoded: try XCTUnwrap(entry["body_base64"] as? String))
            )
            XCTAssertEqual(
                CanonicalRequest.digestBody(body),
                try XCTUnwrap(entry["digest"] as? String),
                name
            )
        }
    }

    // MARK: - Canonical requests

    func testEveryCanonicalRequestVectorMatchesByteForByte() throws {
        let cases = try self.cases(named: "canonical_requests")
        XCTAssertFalse(cases.isEmpty, "the vector file carries no cases")
        for entry in cases {
            let name = try XCTUnwrap(entry["name"] as? String)
            let expected = try XCTUnwrap(
                Data(base64Encoded: try XCTUnwrap(entry["canonical_base64"] as? String))
            )
            let produced = try canonical(entry)
            XCTAssertEqual(produced, expected, name)
            XCTAssertEqual(
                produced.count,
                try XCTUnwrap(entry["canonical_length"] as? Int),
                name
            )
            XCTAssertEqual(
                SHA256.hash(data: produced).map { String(format: "%02x", $0) }.joined(),
                try XCTUnwrap(entry["canonical_sha256"] as? String),
                name
            )
        }
    }

    func testTheRecordedBodyDigestIsTheDigestOfTheRecordedBody() throws {
        // Otherwise a client could reproduce every canonical vector while
        // hashing bodies wrongly, and only fail against a live server.
        for entry in try cases(named: "canonical_requests") {
            let name = try XCTUnwrap(entry["name"] as? String)
            let body = try XCTUnwrap(
                Data(base64Encoded: try XCTUnwrap(entry["body_base64"] as? String))
            )
            XCTAssertEqual(
                CanonicalRequest.digestBody(body),
                try XCTUnwrap(entry["body_digest"] as? String),
                name
            )
        }
    }

    func testBoundaryPairsAreDistinct() throws {
        let pairs = try XCTUnwrap(vectors["distinct_pairs"] as? [[String]])
        XCTAssertFalse(pairs.isEmpty, "the framing claim needs at least one pair")
        var byName: [String: [String: Any]] = [:]
        for entry in try cases(named: "canonical_requests") {
            byName[try XCTUnwrap(entry["name"] as? String)] = entry
        }
        for pair in pairs {
            let left = try canonical(try XCTUnwrap(byName[pair[0]]))
            let right = try canonical(try XCTUnwrap(byName[pair[1]]))
            XCTAssertNotEqual(left, right, "\(pair[0]) vs \(pair[1])")
        }
    }

    func testUnicodeFieldsAreFramedByUTF8ByteLength() throws {
        // A UTF-16 count or a grapheme count produces a shorter prefix here,
        // and that is the mistake this whole file exists to catch early.
        let entry = try XCTUnwrap(
            try cases(named: "canonical_requests").first {
                $0["name"] as? String == "unicode-idempotency-key"
            }
        )
        let key = try XCTUnwrap(entry["idempotency_key"] as? String)
        XCTAssertGreaterThan(key.utf8.count, key.count, "vector lost its multibyte content")
        var framed = Data()
        var length = UInt32(key.utf8.count).bigEndian
        withUnsafeBytes(of: &length) { framed.append(contentsOf: $0) }
        framed.append(Data(key.utf8))
        XCTAssertTrue(try canonical(entry).range(of: framed) != nil)
    }

    func testAnEmptyRevisionStillContributesAZeroLength() throws {
        // The refresh envelope. Dropping the empty field is the single most
        // likely way for a client to break token refresh and nothing else.
        let entry = try XCTUnwrap(
            try cases(named: "canonical_requests").first {
                $0["name"] as? String == "empty-body-refresh"
            }
        )
        let produced = try canonical(entry)
        XCTAssertEqual(produced.suffix(4), Data([0, 0, 0, 0]))
    }

    // MARK: - Signatures

    func testTheSharedSignatureVectorsVerifyWithCryptoKit() throws {
        let signatures = try XCTUnwrap(vectors["signature_vectors"] as? [String: Any])
        XCTAssertEqual(signatures["algorithm"] as? String, "ecdsa-p256-sha256")
        let target = try XCTUnwrap(
            try cases(named: "canonical_requests").first {
                $0["name"] as? String == signatures["canonical_vector"] as? String
            }
        )
        let canonicalBytes = try canonical(target)
        let keyBytes = try XCTUnwrap(
            Data(hex: try XCTUnwrap(signatures["public_key_x963_hex"] as? String))
        )
        // Uncompressed SEC1 / X9.63, which is also what CryptoKit exports.
        XCTAssertEqual(keyBytes.count, 65)
        XCTAssertEqual(keyBytes.first, 0x04)
        let key = try P256.Signing.PublicKey(x963Representation: keyBytes)

        for entry in try XCTUnwrap(signatures["must_verify"] as? [[String: Any]]) {
            let name = try XCTUnwrap(entry["name"] as? String)
            let raw = try XCTUnwrap(Data(hex: try XCTUnwrap(entry["signature_hex"] as? String)))
            let signature = try P256.Signing.ECDSASignature(rawRepresentation: raw)
            XCTAssertTrue(
                key.isValidSignature(signature, for: canonicalBytes),
                "\(name) must verify: \(entry["why"] as? String ?? "")"
            )
        }
        for entry in try XCTUnwrap(signatures["must_not_verify"] as? [[String: Any]]) {
            let name = try XCTUnwrap(entry["name"] as? String)
            let raw = try XCTUnwrap(Data(hex: try XCTUnwrap(entry["signature_hex"] as? String)))
            guard let signature = try? P256.Signing.ECDSASignature(rawRepresentation: raw) else {
                continue  // Refused at parse time, which is a stronger refusal.
            }
            XCTAssertFalse(
                key.isValidSignature(signature, for: canonicalBytes),
                "\(name) must not verify"
            )
        }
    }

    /// What CryptoKit actually exports, asserted rather than assumed.
    ///
    /// This is the open question from #159: the server accepts only a 65-byte
    /// uncompressed SEC1 point, and if CryptoKit produced anything else every
    /// pairing attempt would fail with a 400 that says nothing.
    func testCryptoKitExportsAnUncompressedSEC1Point() throws {
        let key = P256.Signing.PrivateKey()
        let exported = key.publicKey.x963Representation
        XCTAssertEqual(exported.count, 65)
        XCTAssertEqual(exported.first, 0x04)
        // ... and the raw form is the same point without the prefix byte,
        // which is what would silently be sent by a client reaching for the
        // more obvious-sounding property name.
        XCTAssertEqual(key.publicKey.rawRepresentation.count, 64)
        XCTAssertEqual(exported.dropFirst(), key.publicKey.rawRepresentation)
    }

    /// A signature this client produces is 64 raw bytes, never DER.
    func testSignaturesAreRawSixtyFourBytes() throws {
        let key = P256.Signing.PrivateKey()
        let signature = try key.signature(for: Data("canonical".utf8))
        XCTAssertEqual(signature.rawRepresentation.count, 64)
        XCTAssertGreaterThan(signature.derRepresentation.count, 64)
    }

    // MARK: - Helpers

    private func cases(named key: String) throws -> [[String: Any]] {
        try XCTUnwrap(vectors[key] as? [[String: Any]], "missing \(key)")
    }

    private func canonical(_ entry: [String: Any]) throws -> Data {
        // A timestamp is decimal text on the wire whether the vector file
        // records it as a JSON number or a JSON string.
        let timestamp: String
        if let text = entry["timestamp"] as? String {
            timestamp = text
        } else {
            timestamp = String(try XCTUnwrap(entry["timestamp"] as? Int))
        }
        return try CanonicalRequest.bytes(
            method: try XCTUnwrap(entry["method"] as? String),
            path: try XCTUnwrap(entry["path"] as? String),
            namespace: try XCTUnwrap(entry["namespace"] as? String),
            timestamp: timestamp,
            nonce: try XCTUnwrap(entry["nonce"] as? String),
            bodyDigest: try XCTUnwrap(entry["body_digest"] as? String),
            idempotencyKey: try XCTUnwrap(entry["idempotency_key"] as? String),
            revision: try XCTUnwrap(entry["revision"] as? String)
        )
    }
}

extension Data {
    init?(hex: String) {
        guard hex.count % 2 == 0 else { return nil }
        var bytes = [UInt8]()
        bytes.reserveCapacity(hex.count / 2)
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }
}
