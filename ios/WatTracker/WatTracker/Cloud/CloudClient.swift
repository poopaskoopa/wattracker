import Foundation
import Security

/// The three requests the walking skeleton needs, and nothing else.
///
/// Pair once with a code the rider's desktop minted, trade the durable device
/// credential for a short-lived reader context, then read one object.  Every
/// signed request goes through `CanonicalRequest`, which is the whole point of
/// the exercise.
struct CloudClient {
    let baseURL: URL
    let signer: DeviceSigner
    var session: URLSession = .shared

    // MARK: - Wire types

    /// What redeeming a pairing code returns.  `signingNamespace` is an opaque
    /// signing context chosen by the server; the device reproduces it in every
    /// canonical request and can neither pick it nor change it.
    struct PairedDevice: Sendable {
        let credentialID: String
        let subscriptionKey: String
        let signatureAlgorithm: String
        let signingNamespace: String
        let readerContext: String
    }

    enum Failure: Error, CustomStringConvertible {
        case http(Int, String)
        case malformedResponse(String)
        case noProfilePublished

        var description: String {
            switch self {
            case let .http(status, path):
                // 404 is the server's answer to every authentication failure
                // on the read plane -- unknown code, bad signature, stale
                // timestamp, replayed nonce -- on purpose, so that nothing
                // about credential state leaks. It is not a missing route.
                return "HTTP \(status) from \(path)"
            case let .malformedResponse(detail):
                return "Malformed response: \(detail)"
            case .noProfilePublished:
                return "The desktop has not published a profile yet"
            }
        }
    }

    // MARK: - POST /api/v1/devices/pair

    /// Redeem a single-use pairing code for a durable device credential.
    ///
    /// The code is the authorization: this request carries no signature,
    /// because there is not yet a credential to sign with.  What it does carry
    /// is the public half of the key every later request is signed with.
    func pair(code: String) async throws -> PairedDevice {
        let publicKey = signer.publicKeyX963
        let body = try JSONSerialization.data(withJSONObject: [
            "code": code,
            "public_key": publicKey.hexString,
            "signature_algorithm": "ecdsa-p256-sha256",
        ])
        let path = "/api/v1/devices/pair"
        var request = URLRequest(url: try endpoint(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body

        let payload = try await send(request, path: path)
        guard let credentialID = payload["device_credential"] as? String,
              let subscriptionKey = payload["device_subscription_key"] as? String,
              let algorithm = payload["device_signature_algorithm"] as? String,
              let namespace = payload["signing_namespace"] as? String,
              let readerContext = payload["reader_context"] as? String else {
            throw Failure.malformedResponse("pairing response is missing a field")
        }
        return PairedDevice(
            credentialID: credentialID,
            subscriptionKey: subscriptionKey,
            signatureAlgorithm: algorithm,
            signingNamespace: namespace,
            readerContext: readerContext
        )
    }

    // MARK: - POST /api/v1/context/refresh

    /// Trade the device credential for a fresh reader context.
    ///
    /// This is the only request the client signs, and the reason the canonical
    /// request has to be byte-exact.  Its envelope is fixed by the server:
    /// no body, the idempotency key `context-refresh`, and an EMPTY revision
    /// string that still contributes a zero length to the framing.
    func refreshReaderContext(for device: PairedDevice) async throws -> String {
        let path = "/api/v1/context/refresh"
        let timestamp = String(Int(Date().timeIntervalSince1970))
        let nonce = Self.freshNonce()
        let canonical = try CanonicalRequest.bytes(
            method: "POST",
            path: path,
            namespace: device.signingNamespace,
            timestamp: timestamp,
            nonce: nonce,
            bodyDigest: CanonicalRequest.digestBody(Data()),
            idempotencyKey: "context-refresh",
            revision: ""
        )
        let signature = try signer.signature(over: canonical).hexString

        var request = URLRequest(url: try endpoint(path))
        request.httpMethod = "POST"
        request.setValue(device.credentialID, forHTTPHeaderField: "X-Device-Credential")
        request.setValue(timestamp, forHTTPHeaderField: "X-Device-Timestamp")
        request.setValue(nonce, forHTTPHeaderField: "X-Device-Nonce")
        request.setValue(signature, forHTTPHeaderField: "X-Device-Signature")
        // Carried because the gateway in front of a real deployment demands
        // it. The server itself does not check it on this route.
        request.setValue(
            device.subscriptionKey, forHTTPHeaderField: "Ocp-Apim-Subscription-Key"
        )

        let payload = try await send(request, path: path)
        guard let token = payload["reader_context"] as? String else {
            throw Failure.malformedResponse("refresh response has no reader context")
        }
        return token
    }

    // MARK: - GET /api/v1/context/profile

    /// The rider's FTP, as published by their desktop install.
    func fetchFTPWatts(readerContext: String, device: PairedDevice) async throws -> Double {
        let path = "/api/v1/context/profile"
        var request = URLRequest(url: try endpoint(path))
        request.httpMethod = "GET"
        request.setValue("Bearer \(readerContext)", forHTTPHeaderField: "Authorization")
        request.setValue(
            device.subscriptionKey, forHTTPHeaderField: "Ocp-Apim-Subscription-Key"
        )

        let payload = try await send(request, path: path)
        guard let items = payload["items"] as? [[String: Any]] else {
            throw Failure.malformedResponse("profile response has no items")
        }
        guard let profile = items.first(where: { $0["kind"] as? String == "profile" }),
              let data = profile["data"] as? [String: Any] else {
            // An empty collection is a fact, not an error: the rider has not
            // published an FTP. Saying so beats rendering a zero.
            throw Failure.noProfilePublished
        }
        guard let watts = data["ftp_watts"] as? Double else {
            throw Failure.malformedResponse("profile has no ftp_watts")
        }
        return watts
    }

    // MARK: - Plumbing

    /// Join the configured base URL to an absolute request path.
    ///
    /// Built by string rather than `appendingPathComponent` because the path
    /// that goes into the URL and the path that goes into the canonical
    /// request must be the same characters. Percent-encoding one and not the
    /// other is a 401 with no explanation.
    private func endpoint(_ path: String) throws -> URL {
        var base = baseURL.absoluteString
        while base.hasSuffix("/") { base.removeLast() }
        guard let url = URL(string: base + path) else {
            throw Failure.malformedResponse("cannot build a URL for \(path)")
        }
        return url
    }

    private func send(_ request: URLRequest, path: String) async throws -> [String: Any] {
        let (data, response) = try await session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw Failure.http(status, path)
        }
        guard let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Failure.malformedResponse("response body is not a JSON object")
        }
        return payload
    }

    /// A nonce the replay guard has not seen.  Freshness comes from this, not
    /// from the signature: the server keys its replay guard on
    /// (namespace, credential, nonce) and never on signature bytes, which is
    /// what makes accepting a malleable signature safe.
    static func freshNonce() -> String {
        var bytes = [UInt8](repeating: 0, count: 24)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}

extension Data {
    /// Lowercase hex.  The server's regexes accept nothing else.
    var hexString: String {
        map { String(format: "%02x", $0) }.joined()
    }
}
