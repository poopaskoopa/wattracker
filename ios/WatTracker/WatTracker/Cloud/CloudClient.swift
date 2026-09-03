import Foundation
import Security

/// One request each, typed, with nothing remembered between them.
///
/// Pair once with a code the rider's desktop minted, trade the durable device
/// credential for a short-lived reader context, then read a collection. Every
/// signed request goes through `CanonicalRequest`, which is the whole point of
/// the exercise.
///
/// This layer deliberately holds no state: no token, no cache, no retry, no
/// idea what time the last request failed at.  All of that is `CloudSession`,
/// and keeping the two apart is what lets the lifecycle be tested against
/// scripted responses without a network and the requests be read without the
/// lifecycle in the way.
struct CloudClient: Sendable {
    let baseURL: URL
    let signer: DeviceSigner
    var transport: CloudTransport = URLSessionCloudTransport.shared
    /// Injectable so the signed timestamp is a value a test can choose. The
    /// server accepts a five-minute window either side of its own clock.
    var clock: @Sendable () -> Date = { Date() }

    enum Failure: Error, CustomStringConvertible {
        case http(status: Int, path: String, retryAfter: TimeInterval?, serverDate: Date?)
        case malformedResponse(String)
        case noProfilePublished
        /// `SecRandomCopyBytes` refused to fill the nonce. Signing and sending
        /// anyway is the alternative this exists to rule out: the fallback
        /// value is 24 zero bytes, a repeated nonce trips the server's replay
        /// guard, and that 404 is indistinguishable from a rejected credential
        /// -- a strike toward removal for a reason that had nothing to do with
        /// this device's standing.
        case nonceUnavailable(OSStatus)

        var description: String {
            switch self {
            case let .http(status, path, _, _):
                // 404 is the server's answer to every authentication failure
                // on the read plane -- unknown code, revoked device, bad
                // signature, stale timestamp, replayed nonce -- on purpose, so
                // that nothing about credential state leaks. It is not a
                // missing route, and `CloudSession` is where that is acted on.
                return "HTTP \(status) from \(path)"
            case let .malformedResponse(detail):
                return "Malformed response: \(detail)"
            case .noProfilePublished:
                return "The desktop has not published a profile yet"
            case let .nonceUnavailable(status):
                return "Could not generate a signing nonce (OSStatus \(status))"
            }
        }
    }

    // MARK: - POST /api/v1/devices/pair

    /// Redeem a single-use pairing code for a durable device credential.
    ///
    /// The code is the authorization: this request carries no signature,
    /// because there is not yet a credential to sign with.  What it does carry
    /// is the public half of the key every later request is signed with.
    func pair(code: String, label: String? = nil) async throws -> PairingResult {
        let path = "/api/v1/devices/pair"
        var bodyObject: [String: Any] = [
            "code": code,
            "public_key": signer.publicKeyX963.hexString,
            "signature_algorithm": "ecdsa-p256-sha256",
        ]
        if let label { bodyObject["label"] = label }
        let body = try JSONSerialization.data(withJSONObject: bodyObject)
        var request = URLRequest(url: try endpoint(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body

        let payload: PairingResponse = try await send(request, path: path)
        return PairingResult(
            device: PairedDevice(
                credentialID: payload.deviceCredential,
                subscriptionKey: payload.deviceSubscriptionKey,
                signatureAlgorithm: payload.deviceSignatureAlgorithm,
                signingNamespace: payload.signingNamespace,
                capabilities: payload.deviceCapabilities ?? ["read"]
            ),
            readerContext: payload.readerContext,
            expiresIn: payload.expiresIn ?? CloudSession.defaultContextLifetime
        )
    }

    func devices(for device: PairedDevice) async throws -> [CloudDevice] {
        let path = "/api/v1/devices"
        let request = try signedWriterRequest(
            method: "GET", path: path, device: device, idempotencyKey: "device-list"
        )
        let response: DeviceListResponse = try await sendReturningResponse(
            request, path: path
        ).0
        return response.devices
    }

    func revoke(credentialID: String, for device: PairedDevice) async throws {
        let path = "/api/v1/devices/\(credentialID)/revoke"
        let request = try signedWriterRequest(
            method: "POST", path: path, device: device, idempotencyKey: "device-revoke"
        )
        let response: DeviceRevokeResponse = try await sendReturningResponse(
            request, path: path
        ).0
        guard response.revoked else {
            throw Failure.malformedResponse("\(path) did not revoke the device")
        }
    }

    func revoke(credentialID: String, device: PairedDevice) async throws {
        try await revoke(credentialID: credentialID, for: device)
    }

    // MARK: - Signed device administration

    private func signedWriterRequest(
        method: String, path: String, device: PairedDevice, idempotencyKey: String
    ) throws -> URLRequest {
        let timestamp = String(Int(clock().timeIntervalSince1970))
        let nonce = try Self.freshNonce()
        let canonical = try CanonicalRequest.bytes(
            method: method, path: path, namespace: device.signingNamespace,
            timestamp: timestamp, nonce: nonce, bodyDigest: CanonicalRequest.digestBody(Data()),
            idempotencyKey: idempotencyKey, revision: "0"
        )
        var request = URLRequest(url: try endpoint(path))
        request.httpMethod = method
        request.setValue(device.credentialID, forHTTPHeaderField: "X-Writer-Credential")
        request.setValue(timestamp, forHTTPHeaderField: "X-Writer-Timestamp")
        request.setValue(nonce, forHTTPHeaderField: "X-Writer-Nonce")
        request.setValue(idempotencyKey, forHTTPHeaderField: "X-Writer-Idempotency-Key")
        request.setValue("0", forHTTPHeaderField: "X-Writer-Revision")
        request.setValue(try signer.signature(over: canonical).hexString, forHTTPHeaderField: "X-Writer-Signature")
        request.setValue(device.subscriptionKey, forHTTPHeaderField: "Ocp-Apim-Subscription-Key")
        return request
    }

    /// What a refresh produced, and what the server's clock said while doing it.
    struct RefreshOutcome: Sendable {
        let readerContext: String
        let expiresIn: TimeInterval
        let serverDate: Date?
    }

    /// Trade the device credential for a fresh reader context.
    ///
    /// The refresh envelope is fixed by the server, as are the device-list and
    /// revoke envelopes above. Every canonical request has to be byte-exact.
    /// Refresh carries:
    /// no body, the idempotency key `context-refresh`, and an EMPTY revision
    /// string that still contributes a zero length to the framing.
    func refreshReaderContext(for device: PairedDevice) async throws -> RefreshOutcome {
        let path = "/api/v1/context/refresh"
        let timestamp = String(Int(clock().timeIntervalSince1970))
        let nonce = try Self.freshNonce()
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

        let (payload, response): (RefreshResponse, CloudResponse) =
            try await sendReturningResponse(request, path: path)
        return RefreshOutcome(
            readerContext: payload.readerContext,
            expiresIn: payload.expiresIn ?? CloudSession.defaultContextLifetime,
            serverDate: response.serverDate
        )
    }

    // MARK: - GET /api/v1/context/*

    /// One page of a collection.
    ///
    /// `since` and `cursor` are accepted only where the route serves deltas;
    /// the server ignores both elsewhere, and sending them anyway would make a
    /// full read look like a delta to anybody reading the traffic or this
    /// code. `CloudRoute.servesDeltas` is checked by the caller.
    ///
    /// No `limit` is sent: the server's default is already its maximum, and a
    /// smaller one would only buy more round trips for the same objects.
    func collection(
        _ route: CloudRoute,
        readerContext: String,
        device: PairedDevice,
        since: Int? = nil,
        cursor: String? = nil
    ) async throws -> CollectionResponse {
        var query: [URLQueryItem] = []
        if let since { query.append(URLQueryItem(name: "since", value: String(since))) }
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        var request = URLRequest(url: try endpoint(route.path, query: query))
        request.httpMethod = "GET"
        request.setValue("Bearer \(readerContext)", forHTTPHeaderField: "Authorization")
        request.setValue(
            device.subscriptionKey, forHTTPHeaderField: "Ocp-Apim-Subscription-Key"
        )
        return try await send(request, path: route.path)
    }

    /// The rider's FTP, as published by their desktop install.
    ///
    /// Kept for #171's on-device round-trip, which is the only evidence the
    /// pairing, signing and read planes work end to end on real hardware.
    /// It reads the same `profile` object the dashboard does, through the same
    /// decoder, so the two cannot disagree about what a profile is.
    func fetchFTPWatts(readerContext: String, device: PairedDevice) async throws -> Double {
        let response = try await collection(
            .profile, readerContext: readerContext, device: device
        )
        guard let profile = response.items.first(where: { $0.kind == .profile }),
              case let .profile(data) = profile.payload else {
            // An empty collection is a fact, not an error: the rider has not
            // published an FTP. Saying so beats rendering a zero.
            throw Failure.noProfilePublished
        }
        guard let watts = data.resolvedFTP else {
            throw Failure.malformedResponse("profile carries no FTP")
        }
        return watts
    }

    // MARK: - Plumbing

    /// Join the configured base URL to an absolute request path.
    ///
    /// The path is built by string rather than `appendingPathComponent`
    /// because the path that goes into the URL and the path that goes into the
    /// canonical request must be the same characters. Percent-encoding one and
    /// not the other is a 404 with no explanation. The query is appended
    /// through `URLComponents`, which is safe precisely because the server
    /// signs `request.url.path` and never the query.
    private func endpoint(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        var base = baseURL.absoluteString
        while base.hasSuffix("/") { base.removeLast() }
        guard var components = URLComponents(string: base + path) else {
            throw Failure.malformedResponse("cannot build a URL for \(path)")
        }
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else {
            throw Failure.malformedResponse("cannot build a URL for \(path)")
        }
        return url
    }

    private func send<T: Decodable>(_ request: URLRequest, path: String) async throws -> T {
        let (value, _): (T, CloudResponse) =
            try await sendReturningResponse(request, path: path)
        return value
    }

    private func sendReturningResponse<T: Decodable>(
        _ request: URLRequest, path: String
    ) async throws -> (T, CloudResponse) {
        let response = try await transport.send(request)
        guard (200..<300).contains(response.status) else {
            throw Failure.http(
                status: response.status,
                path: path,
                retryAfter: response.retryAfter,
                serverDate: response.serverDate
            )
        }
        do {
            return (try JSONDecoder().decode(T.self, from: response.body), response)
        } catch {
            // The decoding error itself is not carried: it quotes the part of
            // the body it choked on, and on this API the body is the rider's
            // data and, on the refresh route, a bearer token.
            throw Failure.malformedResponse("\(path) returned an unreadable body")
        }
    }

    /// A nonce the replay guard has not seen.  Freshness comes from this, not
    /// from the signature: the server keys its replay guard on
    /// (namespace, credential, nonce) and never on signature bytes, which is
    /// what makes accepting a malleable signature safe.  The status is
    /// checked, not discarded: a failed fill left as 24 zero bytes would
    /// still get signed and sent, and a zero nonce is only ever fresh once.
    static func freshNonce() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 24)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        guard status == errSecSuccess else { throw Failure.nonceUnavailable(status) }
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
