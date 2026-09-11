import XCTest

final class LocalBackendTests: XCTestCase {
    private final class ScriptedLocalTransport: LocalTransport, @unchecked Sendable {
        private let lock = NSLock()
        private var responses: [LocalResponse]
        private(set) var requests: [URLRequest] = []

        init(_ responses: [LocalResponse]) {
            self.responses = responses
        }

        func send(_ request: URLRequest) async throws -> LocalResponse {
            lock.lock()
            requests.append(request)
            let response = responses.removeFirst()
            lock.unlock()
            return response
        }
    }

    private let baseURL = URL(string: "https://desktop.example")!

    func testLocalClientRejectsPlainHTTP() {
        XCTAssertThrowsError(
            try LocalClient(
                baseURL: URL(string: "http://desktop.example")!, token: "secret"
            )
        ) { error in
            guard case LocalClient.Failure.insecureOrInvalidBaseURL = error else {
                return XCTFail("unexpected error: \(error)")
            }
        }
    }

    func testAuthenticateUsesBearerTokenAndRedeemsTicketWithoutPersistingIt() async throws {
        let redeemURL = URL(string: "https://desktop.example/")!
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"one-time-ticket"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            LocalResponse(status: 200, body: Data("<html>home</html>".utf8), url: redeemURL),
        ])
        let client = try LocalClient(
            baseURL: baseURL, token: "device-token", transport: transport
        )

        try await client.authenticate()

        XCTAssertEqual(transport.requests.count, 2)
        XCTAssertEqual(transport.requests[0].httpMethod, "POST")
        XCTAssertEqual(
            transport.requests[0].value(forHTTPHeaderField: "Authorization"),
            "Bearer device-token"
        )
        XCTAssertEqual(transport.requests[1].url?.path, "/connector/session")
        XCTAssertEqual(transport.requests[1].queryItems["token"], "one-time-ticket")
    }

    func testLocalSessionMapsDesktopStateIntoSharedSnapshot() async throws {
        let credential = try LocalCredential(
            baseURL: baseURL.absoluteString, token: "device-token", label: "Mac"
        )
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            LocalResponse(status: 200, body: Data(), url: baseURL.appendingPathComponent("/")),
            LocalResponse(
                status: 200,
                body: Data(#"{"ftp":250,"cp":300,"wprime":18000}"#.utf8),
                url: baseURL.appendingPathComponent("/api/state")
            ),
            LocalResponse(
                status: 200,
                body: Data(#"{"ftp":250,"cp":300,"wprime":18000}"#.utf8),
                url: baseURL.appendingPathComponent("/api/state")
            ),
            LocalResponse(
                status: 200,
                body: Data(#"[{"date":"2026-09-01","ctl":40,"atl":35,"tsb":5,"tss":50}]"#.utf8),
                url: baseURL.appendingPathComponent("/api/load")
            ),
            LocalResponse(
                status: 200,
                body: Data(#"{"measured":[{"t":5,"power":300}],"cp":300,"wprime":18000}"#.utf8),
                url: baseURL.appendingPathComponent("/api/curve")
            ),
        ])
        let store = MemoryLocalCredentialStore()
        let cache = MemorySnapshotCache()
        let session = LocalSession(
            credentials: store,
            cache: cache,
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!,
                    token: value.token,
                    transport: transport
                )
            }
        )

        try await session.pair(host: credential.baseURL, token: credential.token, label: credential.label)
        let snapshot = try await session.load(.dashboard)

        XCTAssertEqual(snapshot.source, .network)
        XCTAssertEqual(snapshot.items.compactMap { item -> TrainingState? in
            guard case let .trainingState(value) = item.payload else { return nil }
            return value
        }.first?.ftp, 250)
        XCTAssertNotNil(cache.load(.dashboard))
        let state = await session.deviceState
        XCTAssertEqual(state, .paired)
    }

    func testARevokedLocalTokenClearsCredentialAndCache() async throws {
        let credential = try LocalCredential(
            baseURL: baseURL.absoluteString, token: "device-token"
        )
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket-1"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            LocalResponse(status: 200, body: Data(), url: baseURL.appendingPathComponent("/")),
            LocalResponse(status: 401, body: Data(), url: baseURL.appendingPathComponent("/api/state")),
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket-2"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            LocalResponse(status: 200, body: Data(), url: baseURL.appendingPathComponent("/")),
            LocalResponse(status: 401, body: Data(), url: baseURL.appendingPathComponent("/api/state")),
        ])
        let store = MemoryLocalCredentialStore(credential: credential)
        let cache = MemorySnapshotCache()
        cache.store(CachedCollection(revision: 0, items: [], storedAt: Date()), for: .dashboard)
        let session = LocalSession(
            credentials: store,
            cache: cache,
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!,
                    token: value.token,
                    transport: transport
                )
            }
        )

        do {
            _ = try await session.load(.dashboard)
            XCTFail("a refused local token must become terminal")
        } catch let failure as CloudSession.Failure {
            guard case .deviceRemoved = failure else {
                return XCTFail("unexpected failure: \(failure)")
            }
        }
        let state = await session.deviceState
        XCTAssertEqual(state, .removed)
        XCTAssertNil(store.load())
        XCTAssertNil(cache.load(.dashboard))
    }
}
