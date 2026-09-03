import Foundation
import CryptoKit
import XCTest

/// A transport that answers from a script instead of a network.
///
/// The whole point of `CloudTransport` being a protocol: the token lifecycle
/// is a state machine about *when* things happen, and driving it through
/// `URLProtocol` or a local server would test the machinery it is standing in
/// for, at the mercy of real timing. Here a test says what the fourth request
/// gets back, and asserts what the fifth one was.
final class ScriptedTransport: CloudTransport, @unchecked Sendable {
    typealias Handler = @Sendable (URLRequest, Int) async throws -> CloudResponse

    private let lock = NSLock()
    private var recorded: [URLRequest] = []
    private let handler: Handler

    init(handler: @escaping Handler) {
        self.handler = handler
    }

    /// Every request that was actually sent, in order.
    var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    var requestCount: Int { requests.count }

    func requests(matching path: String) -> [URLRequest] {
        requests.filter { $0.url?.path == path }
    }

    func send(_ request: URLRequest) async throws -> CloudResponse {
        lock.lock()
        recorded.append(request)
        let index = recorded.count - 1
        lock.unlock()
        return try await handler(request, index)
    }
}

extension CloudResponse {
    static func json(
        _ text: String, status: Int = 200,
        retryAfter: TimeInterval? = nil, serverDate: Date? = nil
    ) -> CloudResponse {
        CloudResponse(
            status: status, body: Data(text.utf8),
            retryAfter: retryAfter, serverDate: serverDate
        )
    }

    static func refused(
        _ status: Int, retryAfter: TimeInterval? = nil, serverDate: Date? = nil
    ) -> CloudResponse {
        CloudResponse(
            status: status, body: Data(#"{"detail":"not found"}"#.utf8),
            retryAfter: retryAfter, serverDate: serverDate
        )
    }
}

/// A clock a test moves by hand.
///
/// Every deadline in `CloudSession` -- expiry, refresh-ahead, the backoff gate
/// -- is read through the injected clock, so "the token expires mid-use" is a
/// line of test code rather than five minutes of waiting.
final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var current: Date

    init(_ start: Date = Date(timeIntervalSince1970: 1_735_689_600)) {
        current = start
    }

    var now: Date {
        lock.lock()
        defer { lock.unlock() }
        return current
    }

    func advance(_ seconds: TimeInterval) {
        lock.lock()
        current = current.addingTimeInterval(seconds)
        lock.unlock()
    }

    var reader: @Sendable () -> Date {
        { [weak self] in self?.now ?? Date() }
    }
}

/// A software P-256 signer.
///
/// The simulator has no Secure Enclave, and these tests are about the request
/// lifecycle rather than about where the key lives -- `DeviceKeyStore` and the
/// shared canonical-request vectors cover that.
struct StubSigner: DeviceSigner {
    let key = P256.Signing.PrivateKey()
    var publicKeyX963: Data { key.publicKey.x963Representation }
    var isHardwareBacked: Bool { false }
    func signature(over data: Data) throws -> Data {
        try key.signature(for: data).rawRepresentation
    }
}

/// A gate a test opens when it chooses, so several callers can be held inside
/// one in-flight request at the same time.
actor RequestGate {
    private var waiting: [CheckedContinuation<Void, Never>] = []
    private var open = false
    private(set) var arrived = 0

    func wait() async {
        arrived += 1
        if open { return }
        await withCheckedContinuation { waiting.append($0) }
    }

    func openGate() {
        open = true
        let pending = waiting
        waiting = []
        for continuation in pending { continuation.resume() }
    }
}

// MARK: - Fixtures

enum CloudFixtures {
    static let namespace = String(repeating: "a", count: 64)

    static let device = PairedDevice(
        credentialID: "credential-1",
        subscriptionKey: "subscription-1",
        signatureAlgorithm: "ecdsa-p256-sha256",
        signingNamespace: namespace
    )

    static func pairingBody(context: String = "context-0") -> String {
        """
        {"device_credential":"credential-1",\
        "device_subscription_key":"subscription-1",\
        "device_signature_algorithm":"ecdsa-p256-sha256",\
        "device_capabilities":["read"],\
        "signing_namespace":"\(namespace)",\
        "reader_context":"\(context)","expires_in":300}
        """
    }

    static func refreshBody(context: String, expiresIn: Int = 300) -> String {
        """
        {"reader_context":"\(context)","expires_in":\(expiresIn),\
        "capabilities":["read"]}
        """
    }

    static func profileItem(id: String = "profile", revision: Int, ftp: Double) -> String {
        """
        {"id":"\(id)","kind":"profile","revision":\(revision),"data":{"ftp":\(ftp)}}
        """
    }

    static func tombstone(id: String, kind: String, revision: Int) -> String {
        """
        {"id":"\(id)","kind":"\(kind)","revision":\(revision),"data":{},"deleted":true}
        """
    }

    static func collection(
        items: [String], revision: Int?, nextCursor: String? = nil
    ) -> String {
        let revisionText = revision.map(String.init) ?? "null"
        let cursorText = nextCursor.map { "\"\($0)\"" } ?? "null"
        return """
        {"items":[\(items.joined(separator: ","))],\
        "revision":\(revisionText),"next_cursor":\(cursorText)}
        """
    }

    static func item(id: String, kind: String, revision: Int, data: String) -> CloudItem {
        let text = """
        {"id":"\(id)","kind":"\(kind)","revision":\(revision),"data":\(data)}
        """
        // Force-tried rather than thrown: a fixture that does not decode is a
        // broken test, not a condition under test.
        return try! JSONDecoder().decode(CloudItem.self, from: Data(text.utf8))
    }
}

extension URLRequest {
    var bearerToken: String? {
        value(forHTTPHeaderField: "Authorization")?
            .replacingOccurrences(of: "Bearer ", with: "")
    }

    var queryItems: [String: String] {
        guard let url,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let items = components.queryItems else { return [:] }
        var out: [String: String] = [:]
        for item in items { out[item.name] = item.value }
        return out
    }
}
