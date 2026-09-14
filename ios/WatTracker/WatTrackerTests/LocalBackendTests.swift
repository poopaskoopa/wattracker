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

    private final class DelayedLocalTransport: LocalTransport, @unchecked Sendable {
        let delay: UInt64
        init(delay: UInt64) { self.delay = delay }
        func send(_ request: URLRequest) async throws -> LocalResponse {
            try await Task.sleep(nanoseconds: delay)
            throw URLError(.timedOut)
        }
    }

    private let baseURL = URL(string: "https://desktop.example")!

    func testProbeIsBoundedAndPreservesCredentialAndCacheOnTimeout() async throws {
        let credential = try LocalCredential(baseURL: baseURL.absoluteString, token: "device-token")
        let store = MemoryLocalCredentialStore(credential: credential)
        let cache = MemorySnapshotCache()
        cache.store(CachedCollection(revision: 1, items: [], storedAt: Date()), for: .dashboard)
        let session = LocalSession(
            credentials: store, cache: cache,
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: DelayedLocalTransport(delay: 500_000_000))
            }
        )
        let started = Date()
        let reachable = await session.probe(timeout: 0.02)
        XCTAssertFalse(reachable)
        XCTAssertLessThan(Date().timeIntervalSince(started), 0.25)
        XCTAssertEqual(await session.deviceState, .paired)
        XCTAssertNotNil(store.load())
        XCTAssertNotNil(cache.load(.dashboard))
    }

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

    func testAuthenticateStopsAtRedeemRedirectAndRequiresSessionCookie() async throws {
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"one-time-ticket"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            successfulRedeem(),
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

    func testAuthenticateAcceptsAbsoluteSameOriginRootLanding() async throws {
        let baseURL = URL(string: "https://desktop.example/")!
        for location in ["https://desktop.example/", "https://desktop.example"] {
            let transport = ScriptedLocalTransport([
                LocalResponse(
                    status: 200,
                    body: Data(#"{"ticket":"one-time-ticket"}"#.utf8),
                    url: baseURL.appendingPathComponent("/api/connector/session")
                ),
                LocalResponse(
                    status: 303, body: Data(),
                    url: baseURL.appendingPathComponent("/connector/session"),
                    location: location,
                    setCookie: "session=authenticated; Secure; HttpOnly"
                ),
            ])
            let client = try LocalClient(
                baseURL: baseURL, token: "device-token", transport: transport
            )

            try await client.authenticate()

            XCTAssertEqual(transport.requests.count, 2)
        }
    }

    func testAuthenticateRejectsUntrustedOrUnexpectedLandings() async throws {
        let baseURL = URL(string: "https://desktop.example/")!
        let rejectedLocations: [(name: String, value: String?)] = [
            ("network-path cross-origin", "//attacker.example/"),
            ("scheme mismatch", "http://desktop.example/"),
            ("host mismatch", "https://attacker.example/"),
            ("port mismatch", "https://desktop.example:8443/"),
            ("unexpected path", "/login"),
            ("encoded path", "https://desktop.example/%2F"),
            ("double-slash path", "https://desktop.example//"),
            ("query", "/?next=/"),
            ("fragment", "/#landing"),
            ("userinfo", "https://rider@desktop.example/"),
            ("malformed", "https://[::1"),
            ("empty", ""),
            ("missing", nil),
        ]

        for rejected in rejectedLocations {
            let transport = ScriptedLocalTransport([
                LocalResponse(
                    status: 200,
                    body: Data(#"{"ticket":"one-time-ticket"}"#.utf8),
                    url: baseURL.appendingPathComponent("/api/connector/session")
                ),
                LocalResponse(
                    status: 303, body: Data(),
                    url: baseURL.appendingPathComponent("/connector/session"),
                    location: rejected.value,
                    setCookie: "session=authenticated; Secure; HttpOnly"
                ),
            ])
            let client = try LocalClient(
                baseURL: baseURL, token: "device-token", transport: transport
            )

            do {
                try await client.authenticate()
                XCTFail("accepted \(rejected.name) landing")
            } catch let failure as LocalClient.Failure {
                guard case .unexpectedLanding = failure else {
                    return XCTFail("\(rejected.name) produced unexpected failure: \(failure)")
                }
            }
        }
    }

    func testRedirectDelegateDoesNotFollowRedeemIntoDashboard() throws {
        let delegate = LocalRedirectDelegate(origin: baseURL)
        let redeemURL = try XCTUnwrap(
            URL(string: "https://desktop.example/connector/session?token=ticket")
        )
        let task = URLSession.shared.dataTask(with: redeemURL)
        let response = try XCTUnwrap(
            HTTPURLResponse(
                url: redeemURL, statusCode: 303, httpVersion: nil,
                headerFields: ["Location": "/"]
            )
        )
        var redirectedRequest: URLRequest? = URLRequest(url: baseURL)

        delegate.urlSession(
            .shared, task: task, willPerformHTTPRedirection: response,
            newRequest: URLRequest(url: baseURL)
        ) { redirectedRequest = $0 }

        XCTAssertNil(redirectedRequest)
        task.cancel()
    }

    func test403KeepsCredentialAndReturnsRequestedCache() async throws {
        let credential = try LocalCredential(
            baseURL: baseURL.absoluteString, token: "device-token"
        )
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            successfulRedeem(),
            LocalResponse(
                status: 403, body: Data(), url: baseURL.appendingPathComponent("/api/state")
            ),
        ])
        let store = MemoryLocalCredentialStore(credential: credential)
        let cache = MemorySnapshotCache()
        let cachedState = CloudFixtures.item(
            id: "training-state", kind: "training_state", revision: 0,
            data: #"{"ftp":250}"#
        )
        cache.store(
            CachedCollection(revision: 0, items: [cachedState], storedAt: Date()),
            for: .dashboard
        )
        let session = LocalSession(
            credentials: store,
            cache: cache,
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!, token: value.token, transport: transport
                )
            }
        )

        let snapshot = try await session.load(.dashboard)

        XCTAssertEqual(snapshot.source, .cache)
        XCTAssertEqual(snapshot.items, [cachedState])
        let state = await session.deviceState
        XCTAssertEqual(state, .paired)
        XCTAssertNotNil(store.load())
        XCTAssertNotNil(cache.load(.dashboard))
    }

    func testSameOriginAuthenticationLandingIsRetriedWithoutRemovingDevice() async throws {
        let credential = try LocalCredential(
            baseURL: baseURL.absoluteString, token: "device-token"
        )
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket-1"}"#.utf8),
                url: baseURL.appendingPathComponent("/api/connector/session")
            ),
            LocalResponse(
                status: 303, body: Data(),
                url: baseURL.appendingPathComponent("/connector/session"),
                location: "/login"
            ),
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket-2"}"#.utf8),
                url: baseURL.appendingPathComponent("/api/connector/session")
            ),
            successfulRedeem(),
            LocalResponse(
                status: 403, body: Data(),
                url: baseURL.appendingPathComponent("/api/state")
            ),
        ])
        let store = MemoryLocalCredentialStore(credential: credential)
        let cache = MemorySnapshotCache()
        let cachedState = CloudFixtures.item(
            id: "training-state", kind: "training_state", revision: 0,
            data: #"{"ftp":250}"#
        )
        cache.store(
            CachedCollection(revision: 0, items: [cachedState], storedAt: Date()),
            for: .dashboard
        )
        let session = LocalSession(
            credentials: store,
            cache: cache,
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!, token: value.token,
                    transport: transport
                )
            }
        )

        let snapshot = try await session.load(.dashboard)

        XCTAssertEqual(snapshot.source, .cache)
        XCTAssertEqual(snapshot.items, [cachedState])
        XCTAssertEqual(
            transport.requests.filter { $0.url?.path == "/api/connector/session" }.count,
            2,
            "the first authentication failure must be retried"
        )
        let state = await session.deviceState
        XCTAssertEqual(state, .paired)
        XCTAssertNotNil(store.load())
        XCTAssertNotNil(cache.load(.dashboard))
    }

    func testUnexpectedLandingThenOneUnauthorizedKeepsCredentialAndCache() async throws {
        let credential = try LocalCredential(
            baseURL: baseURL.absoluteString, token: "device-token"
        )
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket-1"}"#.utf8),
                url: baseURL.appendingPathComponent("/api/connector/session")
            ),
            successfulRedeem(),
            LocalResponse(
                status: 200, body: Data("<html>login</html>".utf8),
                url: baseURL.appendingPathComponent("/login")
            ),
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket-2"}"#.utf8),
                url: baseURL.appendingPathComponent("/api/connector/session")
            ),
            successfulRedeem(),
            LocalResponse(
                status: 401, body: Data(),
                url: baseURL.appendingPathComponent("/api/state")
            ),
        ])
        let store = MemoryLocalCredentialStore(credential: credential)
        let cache = MemorySnapshotCache()
        let cachedState = CloudFixtures.item(
            id: "training-state", kind: "training_state", revision: 0,
            data: #"{"ftp":250}"#
        )
        cache.store(
            CachedCollection(revision: 0, items: [cachedState], storedAt: Date()),
            for: .dashboard
        )
        let session = LocalSession(
            credentials: store,
            cache: cache,
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!, token: value.token,
                    transport: transport
                )
            }
        )

        let snapshot = try await session.load(.dashboard)

        XCTAssertEqual(snapshot.source, .cache)
        XCTAssertEqual(snapshot.items, [cachedState])
        let state = await session.deviceState
        XCTAssertEqual(state, .paired)
        XCTAssertNotNil(store.load())
        XCTAssertNotNil(cache.load(.dashboard))
    }

    func testLocalActivityStreamTimeIsNormalizedToSeconds() async throws {
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"t":[0,0.5,1.25],"power":[100,200,300]}"#.utf8),
                url: baseURL.appendingPathComponent("/api/activity/7")
            ),
        ])
        let client = try LocalClient(
            baseURL: baseURL, token: "device-token", transport: transport
        )

        let streams = try await client.activityStreams(7)

        XCTAssertEqual(streams.streams.time!, [0, 30, 75])
    }

    func testCalendarRequestsAndFallsBackToTheRequestedMonthOnly() async throws {
        let credential = try LocalCredential(
            baseURL: baseURL.absoluteString, token: "device-token"
        )
        let january = CalendarMonth(year: 2026, month: 1)
        let february = CalendarMonth(year: 2026, month: 2)
        let transport = ScriptedLocalTransport([
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            successfulRedeem(),
            LocalResponse(
                status: 200,
                body: Data(#"{"weeks":[[{"date":"2026-01-02"},{"date":"2025-12-31"}]]}"#.utf8),
                url: baseURL.appendingPathComponent("/api/calendar")
            ),
            LocalResponse(
                status: 200,
                body: Data(#"{"weeks":[[{"date":"2026-02-03"},{"date":"2026-01-31"}]]}"#.utf8),
                url: baseURL.appendingPathComponent("/api/calendar")
            ),
            LocalResponse(
                status: 403, body: Data(), url: baseURL.appendingPathComponent("/api/calendar")
            ),
        ])
        let session = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: credential),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!, token: value.token, transport: transport
                )
            }
        )

        let januarySnapshot = try await session.load(.calendar, month: january)
        let februarySnapshot = try await session.load(.calendar, month: february)
        let fallback = try await session.load(.calendar, month: january)

        XCTAssertEqual(januarySnapshot.items.compactMap(Self.calendarDate), ["2026-01-02"])
        XCTAssertEqual(februarySnapshot.items.compactMap(Self.calendarDate), ["2026-02-03"])
        XCTAssertEqual(fallback.source, .cache)
        XCTAssertEqual(fallback.items.compactMap(Self.calendarDate), ["2026-01-02"])
        XCTAssertEqual(
            session.cached(.calendar, month: january)?.items.compactMap(Self.calendarDate),
            ["2026-01-02"]
        )
        XCTAssertEqual(
            session.cached(.calendar, month: february)?.items.compactMap(Self.calendarDate),
            ["2026-02-03"]
        )
        let calendarRequests = transport.requests.filter { $0.url?.path == "/api/calendar" }
        XCTAssertEqual(calendarRequests[0].queryItems["year"], "2026")
        XCTAssertEqual(calendarRequests[0].queryItems["month"], "1")
        XCTAssertEqual(calendarRequests[1].queryItems["month"], "2")
        XCTAssertEqual(calendarRequests[2].queryItems["month"], "1")
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
            successfulRedeem(),
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
            successfulRedeem(),
            LocalResponse(status: 401, body: Data(), url: baseURL.appendingPathComponent("/api/state")),
            LocalResponse(
                status: 200,
                body: Data(#"{"ticket":"ticket-2"}"#.utf8),
                url: URL(string: "https://desktop.example/api/connector/session")!
            ),
            successfulRedeem(),
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

    private static func calendarDate(_ item: CloudItem) -> String? {
        guard case let .calendarDay(day) = item.payload else { return nil }
        return day.date
    }

    private func successfulRedeem() -> LocalResponse {
        LocalResponse(
            status: 303, body: Data(),
            url: baseURL.appendingPathComponent("/connector/session"),
            location: "/", setCookie: "session=authenticated; Secure; HttpOnly"
        )
    }
}
