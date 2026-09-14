import XCTest

/// The gate's four states, and the transitions between them that a rider
/// actually performs.
///
/// What is testable here and what is not is worth stating, because #234 is
/// explicit that most of the pairing screen cannot be checked by CI: the
/// camera, the permission prompt and the two-idiom layout all need a device.
/// The state machine underneath them does not, and it is the half where a
/// mistake is silent -- a gate that fails to notice a revocation shows the
/// rider's data on a phone the server has already cut off.
@MainActor
final class SessionGateTests: XCTestCase {
    private let baseURL = URL(string: "https://api.example.invalid")!

    private struct Harness {
        let gate: SessionGate
        let session: CloudSession
        let transport: ScriptedTransport
        let clock: TestClock
        let credentials: MemoryDeviceCredentialStore
        let cache: MemorySnapshotCache
    }

    private final class MemoryPreferenceStore: SessionGate.PreferenceStore, @unchecked Sendable {
        var backend: SessionGate.Backend?
        var legacyBackend: SessionGate.Backend?
        private(set) var savedBackends: [SessionGate.Backend] = []

        init(_ backend: SessionGate.Backend? = nil) {
            self.backend = backend
        }

        func loadBackendOverride() -> SessionGate.Backend? { backend }
        func saveBackendOverride(_ backend: SessionGate.Backend) {
            self.backend = backend
            savedBackends.append(backend)
        }
        func clearBackendOverride() { backend = nil }
        func consumeLegacyBackend() -> SessionGate.Backend? {
            defer { legacyBackend = nil }
            return legacyBackend
        }
    }

    private final class FakePathMonitor: SessionPathMonitor, @unchecked Sendable {
        private var handler: (@Sendable () -> Void)?
        private(set) var startCount = 0
        func start(onChange: @escaping @Sendable () -> Void) {
            startCount += 1
            handler = onChange
        }
        func cancel() { handler = nil }
        func trigger() { handler?() }
    }

    private final class LocalProbeTransport: LocalTransport, @unchecked Sendable {
        private let lock = NSLock()
        private var reachable = true
        private(set) var requests = [URLRequest]()

        func setReachable(_ value: Bool) {
            lock.lock(); reachable = value; lock.unlock()
        }

        func send(_ request: URLRequest) async throws -> LocalResponse {
            lock.lock()
            requests.append(request)
            let isReachable = reachable
            lock.unlock()
            guard isReachable else { throw URLError(.cannotConnectToHost) }
            if request.url?.path == "/api/connector/session" {
                return LocalResponse(
                    status: 200, body: Data(#"{"ticket":"ticket"}"#.utf8),
                    url: request.url!
                )
            }
            if request.url?.path == "/connector/session" {
                return LocalResponse(
                    status: 303, body: Data(), url: request.url!, location: "/",
                    setCookie: "session=authenticated; Secure; HttpOnly"
                )
            }
            return LocalResponse(
                status: 200, body: Data(#"{"ftp":250}"#.utf8), url: request.url!
            )
        }
    }

    private final class BlockingLocalProbeTransport: LocalTransport, @unchecked Sendable {
        let gate = RequestGate()

        func send(_ request: URLRequest) async throws -> LocalResponse {
            await gate.wait()
            if request.url?.path == "/api/connector/session" {
                return LocalResponse(
                    status: 200, body: Data(#"{"ticket":"ticket"}"#.utf8),
                    url: request.url!
                )
            }
            if request.url?.path == "/connector/session" {
                return LocalResponse(
                    status: 303, body: Data(), url: request.url!, location: "/",
                    setCookie: "session=authenticated; Secure; HttpOnly"
                )
            }
            return LocalResponse(
                status: 200, body: Data(#"{"ftp":250}"#.utf8), url: request.url!
            )
        }
    }

    private final class StaggeredLocalProbeTransport: LocalTransport, @unchecked Sendable {
        let firstProbeGate = RequestGate()
        let secondProbeGate = RequestGate()
        private let lock = NSLock()
        private var staggered = false
        private var probeCount = 0

        func beginStaggeredProbes() {
            lock.lock()
            staggered = true
            probeCount = 0
            lock.unlock()
        }

        func send(_ request: URLRequest) async throws -> LocalResponse {
            lock.lock()
            let probeNumber: Int?
            if staggered, request.url?.path == "/api/state" {
                probeCount += 1
                probeNumber = probeCount
            } else {
                probeNumber = nil
            }
            lock.unlock()

            if probeNumber == 1 {
                await firstProbeGate.wait()
                throw URLError(.timedOut)
            }
            if probeNumber == 2 {
                await secondProbeGate.wait()
            }
            if request.url?.path == "/api/connector/session" {
                return LocalResponse(
                    status: 200, body: Data(#"{"ticket":"ticket"}"#.utf8),
                    url: request.url!
                )
            }
            if request.url?.path == "/connector/session" {
                return LocalResponse(
                    status: 303, body: Data(), url: request.url!, location: "/",
                    setCookie: "session=authenticated; Secure; HttpOnly"
                )
            }
            return LocalResponse(
                status: 200, body: Data(#"{"ftp":250}"#.utf8), url: request.url!
            )
        }
    }

    private func harness(
        paired: Bool,
        clock: TestClock = TestClock(),
        handler: @escaping ScriptedTransport.Handler
    ) -> Harness {
        let transport = ScriptedTransport(handler: handler)
        let credentials = MemoryDeviceCredentialStore(
            device: paired ? CloudFixtures.device : nil
        )
        let cache = MemorySnapshotCache()
        let session = CloudSession(
            client: CloudClient(
                baseURL: baseURL, signer: StubSigner(),
                transport: transport, clock: clock.reader
            ),
            credentials: credentials,
            cache: cache,
            clock: clock.reader
        )
        return Harness(
            gate: SessionGate(
                makeSession: { session }, preferences: MemoryPreferenceStore()
            ),
            session: session,
            transport: transport,
            clock: clock,
            credentials: credentials,
            cache: cache
        )
    }

    // MARK: - Where a launch lands

    func testAFreshInstallStartsUnpaired() async {
        let rig = harness(paired: false) { _, _ in .refused(404) }
        XCTAssertEqual(rig.gate.phase, .starting)
        await rig.gate.start()
        XCTAssertEqual(rig.gate.phase, .unpaired)
        XCTAssertEqual(rig.transport.requestCount, 0)
    }

    func testAStoredCredentialStartsPaired() async {
        let rig = harness(paired: true) { _, _ in .refused(404) }
        await rig.gate.start()
        XCTAssertEqual(rig.gate.phase, .paired)
        // A launch reads the keychain and nothing else: a device that opens the
        // app on a train must not need the network to reach its cached data.
        XCTAssertEqual(rig.transport.requestCount, 0)
    }

    func testColdLaunchPreservesPersistedCloudPreference() async {
        let preference = MemoryPreferenceStore(.cloud)
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let transport = LocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(
                credential: try? LocalCredential(
                    baseURL: "https://desktop.example", token: "token"
                )
            ),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: transport)
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session },
            makeLocalSession: { local },
            preferences: preference
        )

        await gate.start()

        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertEqual(gate.phase, .paired)
    }

    func testAutomaticReevaluationFallsBackToCloudWhenLocalIsUnavailable() async {
        let preference = MemoryPreferenceStore()
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let transport = LocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: transport)
            }
        )
        let gate = SessionGate(makeSession: { cloud.session }, makeLocalSession: { local },
                               preferences: preference)
        await gate.start()
        await gate.automaticReevaluate()
        XCTAssertEqual(gate.backend, .local)
        transport.setReachable(false)
        await gate.automaticReevaluate()
        XCTAssertEqual(gate.backend, .cloud)
        let state = await local.deviceState
        XCTAssertEqual(state, .paired)
    }

    func testManualBackendOverrideSurvivesAutomaticReevaluationAndPathChange() async {
        let monitor = FakePathMonitor()
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let transport = LocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: transport)
            }
        )
        let preference = MemoryPreferenceStore()
        let gate = SessionGate(makeSession: { cloud.session }, makeLocalSession: { local },
                               preferences: preference, pathMonitor: monitor)
        await gate.start()
        await gate.select(.cloud)
        transport.setReachable(true)
        monitor.trigger()
        await Task.yield()
        XCTAssertEqual(gate.backend, .cloud)

        await gate.select(.automatic)
        XCTAssertEqual(gate.selection, .automatic)
        XCTAssertEqual(gate.backend, .local)
        XCTAssertNil(preference.backend)
    }

    func testManualSelectionDuringLocalProbeCannotBeOverwritten() async {
        let monitor = FakePathMonitor()
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let transport = BlockingLocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!,
                    token: value.token,
                    transport: transport
                )
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session },
            makeLocalSession: { local },
            preferences: MemoryPreferenceStore(),
            pathMonitor: monitor
        )

        let startTask = Task { await gate.start() }
        guard await transport.gate.waitForArrival(timeout: 1) else {
            return XCTFail("local probe did not start")
        }
        await gate.select(.cloud)
        await transport.gate.openGate()
        await startTask.value

        XCTAssertEqual(gate.backend, .cloud)
    }

    func testAnOlderAutomaticProbeCannotOverwriteANewerPathEvaluation() async {
        let monitor = FakePathMonitor()
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let transport = StaggeredLocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!,
                    token: value.token,
                    transport: transport
                )
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session },
            makeLocalSession: { local },
            preferences: MemoryPreferenceStore(),
            pathMonitor: monitor
        )
        await gate.start()
        transport.beginStaggeredProbes()

        let older = Task { await gate.automaticReevaluate() }
        guard await transport.firstProbeGate.waitForArrival(timeout: 1) else {
            return XCTFail("the older local probe did not start")
        }
        let newer = Task { await gate.automaticReevaluate() }
        await Task.yield()

        await transport.firstProbeGate.openGate()
        guard await transport.secondProbeGate.waitForArrival(timeout: 1) else {
            return XCTFail("the coalesced local probe did not start")
        }
        await transport.secondProbeGate.openGate()
        await older.value
        await newer.value
        XCTAssertEqual(gate.backend, .local)
    }

    func testInjectedPreferenceStoreRestoresOnlyTheBackendValue() {
        let preference = MemoryPreferenceStore(.local)

        let gate = SessionGate(preferences: preference)

        XCTAssertEqual(gate.backend, .local)
        XCTAssertEqual(preference.loadBackendOverride(), .local)
    }

    func testLegacyPreferenceIsConsumedButDoesNotSelectBackend() async {
        let preference = MemoryPreferenceStore()
        preference.legacyBackend = .local
        let rig = harness(paired: true) { _, _ in .refused(404) }
        let gate = SessionGate(makeSession: { rig.session }, preferences: preference)

        await gate.start()

        XCTAssertEqual(gate.selection, .automatic)
        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertNil(preference.legacyBackend)
        XCTAssertNil(preference.backend)
    }

    func testPairingScreenCanReturnFromUnpairedLocalToPairedCloud() async {
        let preference = MemoryPreferenceStore()
        let monitor = FakePathMonitor()
        let rig = harness(paired: true) { _, _ in .refused(404) }
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(), cache: MemorySnapshotCache()
        )
        let gate = SessionGate(
            makeSession: { rig.session },
            makeLocalSession: { local },
            preferences: preference,
            pathMonitor: monitor
        )
        await gate.start()

        await gate.selectBackend(.local)
        monitor.trigger()
        await Task.yield()

        XCTAssertEqual(gate.backend, .local)
        XCTAssertEqual(gate.phase, .unpaired)
        XCTAssertNil(preference.backend)
        XCTAssertNil(gate.lastSuccess)

        let model = PairingModel()
        model.appeared(gate: gate)
        XCTAssertEqual(model.backend, .local)
        await model.selectBackend(.cloud, gate: gate)

        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertEqual(gate.phase, .paired)
        XCTAssertNil(preference.backend)
    }

    func testRemovingLocalFallsBackToPairedCloud() async {
        let preference = MemoryPreferenceStore(.local)
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let monitor = FakePathMonitor()
        let transport = LocalProbeTransport()
        transport.setReachable(false)
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(
                credential: try? LocalCredential(
                    baseURL: "https://desktop.example", token: "token"
                )
            ),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(
                    baseURL: URL(string: value.baseURL)!,
                    token: value.token,
                    transport: transport
                )
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session },
            makeLocalSession: { local },
            preferences: preference,
            pathMonitor: monitor
        )
        await gate.start()
        await gate.automaticReevaluate()

        await gate.selectBackend(.local)
        try? await gate.removeDevice()

        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertEqual(gate.phase, .paired)
        XCTAssertEqual(preference.backend, .local)
    }

    func testAKeyThatCannotBeCreatedIsUnusable() async {
        struct NoKey: Error {}
        let gate = SessionGate(makeSession: { throw NoKey() })
        await gate.start()
        guard case .unusable = gate.phase else {
            return XCTFail("expected .unusable, got \(gate.phase)")
        }
        XCTAssertNil(gate.session)
    }

    func testMissingSelectedLocalSessionDoesNotLeaveGateStarting() async {
        struct NoLocalStore: Error {}
        let preference = MemoryPreferenceStore(.local)
        let rig = harness(paired: false) { _, _ in .refused(404) }
        let gate = SessionGate(
            makeSession: { rig.session },
            makeLocalSession: { throw NoLocalStore() },
            preferences: preference
        )

        await gate.start()

        XCTAssertEqual(gate.backend, .local)
        XCTAssertEqual(gate.phase, .unpaired)
        XCTAssertNil(gate.lastSuccess)
    }

    // MARK: - Pairing

    func testASuccessfulPairingMovesTheGateToPaired() async throws {
        let rig = harness(paired: false) { request, _ in
            XCTAssertEqual(request.url?.path, "/api/v1/devices/pair")
            return .json(CloudFixtures.pairingBody())
        }
        await rig.gate.start()
        XCTAssertEqual(rig.gate.phase, .unpaired)

        try await rig.gate.pair(code: "ABCD-EFGH-JKMN", label: "TR1 iPad")

        XCTAssertEqual(rig.gate.phase, .paired)
        XCTAssertNotNil(rig.credentials.load())
        let body = try XCTUnwrap(rig.transport.requests.first?.httpBody)
        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        )
        // The label the rider typed is what the desktop's device list shows, so
        // it has to actually leave the device.
        XCTAssertEqual(json["label"] as? String, "TR1 iPad")
    }

    func testPairThenAutomaticClearsTheTemporarySelection() async throws {
        let preference = MemoryPreferenceStore()
        let rig = harness(paired: false) { _, _ in .json(CloudFixtures.pairingBody()) }
        let gate = SessionGate(makeSession: { rig.session }, preferences: preference)
        await gate.start()

        try await gate.pair(code: "ABCD-EFGH-JKMN", label: nil)
        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertEqual(gate.selection, .automatic)
        XCTAssertNil(preference.backend)

        await gate.select(.automatic)
        XCTAssertEqual(gate.selection, .automatic)
        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertNil(preference.backend)
    }

    func testAutomaticClearsAPersistedOverrideForAnUnpairedBackend() async {
        let preference = MemoryPreferenceStore()
        let cloud = harness(paired: false) { _, _ in .refused(404) }
        let transport = LocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: transport)
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session }, makeLocalSession: { local },
            preferences: preference
        )

        await gate.start()
        await gate.select(.cloud)
        XCTAssertEqual(gate.selection, .cloud)
        XCTAssertEqual(preference.backend, .cloud)
        XCTAssertEqual(gate.phase, .unpaired)

        await gate.select(.automatic)

        XCTAssertEqual(gate.selection, .automatic)
        XCTAssertEqual(gate.backend, .local)
        XCTAssertEqual(gate.phase, .paired)
        XCTAssertNil(preference.backend)
    }

    func testARefusedPairingLeavesTheGateOnThePairingScreen() async {
        let rig = harness(paired: false) { _, _ in .refused(404) }
        await rig.gate.start()

        do {
            try await rig.gate.pair(code: "ABCD-EFGH-JKMN", label: "TR1 iPad")
            XCTFail("a refused code must not pair")
        } catch {
            // The screen renders this through `PairingFailureMessage`; that it
            // throws at all is what keeps the rider on the pairing screen.
        }
        XCTAssertEqual(rig.gate.phase, .unpaired)
        XCTAssertNil(rig.credentials.load())
    }

    func testFailedLocalPairingDoesNotPersistAttemptedBackend() async {
        let preference = MemoryPreferenceStore(.cloud)
        let rig = harness(paired: true) { _, _ in .refused(404) }
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(),
            cache: MemorySnapshotCache(),
            makeClient: { _ in throw URLError(.cannotConnectToHost) }
        )
        let gate = SessionGate(
            makeSession: { rig.session },
            makeLocalSession: { local },
            preferences: preference
        )
        await gate.start()
        let savesBeforePairing = preference.savedBackends

        do {
            try await gate.pairLocal(
                host: "https://desktop.example", token: "device-token", label: nil
            )
            XCTFail("a failed local pairing must throw")
        } catch {}

        XCTAssertEqual(preference.savedBackends, savesBeforePairing)
        XCTAssertEqual(preference.backend, .cloud)
    }

    // MARK: - Leaving

    func testRemovingThisDeviceRevokesThenReturnsToPairing() async throws {
        let rig = harness(paired: true) { request, _ in
            XCTAssertEqual(
                request.url?.path, "/api/v1/devices/credential-1/revoke"
            )
            return .json(#"{"revoked":true}"#)
        }
        await rig.gate.start()
        rig.cache.store(
            CachedCollection(revision: 4, items: [], storedAt: rig.clock.now),
            for: .dashboard
        )

        try await rig.gate.removeDevice()

        XCTAssertEqual(rig.gate.phase, .unpaired)
        XCTAssertNil(rig.credentials.load())
        XCTAssertNil(rig.cache.load(.dashboard))
        XCTAssertEqual(rig.transport.requestCount, 1)
    }

    func testAFailedRevokeKeepsTheDevicePaired() async {
        // The server refusing the revoke means this device still has access,
        // and a gate that moved to `unpaired` anyway would tell the rider they
        // had removed a device that is still reading their data.
        let rig = harness(paired: true) { _, _ in .refused(500) }
        await rig.gate.start()

        do {
            try await rig.gate.removeDevice()
            XCTFail("a refused revoke must not report success")
        } catch {}

        XCTAssertEqual(rig.gate.phase, .paired)
        XCTAssertNotNil(rig.credentials.load())
    }

    // MARK: - Being removed from the other end

    func testAServerSideRevokeReachesCloudSessionWhenLocalBackendSelected() async {
        let clock = TestClock()
        let rig = harness(paired: true, clock: clock) { _, _ in
            // Every authentication failure on this plane is a 404; the `Date`
            // header is what lets the session rule its own clock out.
            .refused(404, serverDate: clock.now)
        }
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: LocalProbeTransport())
            }
        )
        let gate = SessionGate(
            makeSession: { rig.session }, makeLocalSession: { local },
            preferences: MemoryPreferenceStore(.local)
        )
        rig.cache.store(
            CachedCollection(revision: 4, items: [], storedAt: clock.now),
            for: .dashboard
        )
        await gate.start()
        XCTAssertEqual(gate.backend, .local)
        let requestsBeforeProbe = rig.transport.requestCount

        // One refusal is also what a deployment mid-restart looks like, so it
        // must not be enough.
        await gate.probe()
        XCTAssertGreaterThan(rig.transport.requestCount, requestsBeforeProbe)
        XCTAssertEqual(gate.phase, .paired)

        // Past the backoff the session enforces between the two strikes.
        clock.advance(400)
        let requestsBeforeSecondProbe = rig.transport.requestCount
        await gate.probe()
        XCTAssertGreaterThan(rig.transport.requestCount, requestsBeforeSecondProbe)
        let cloudState = await rig.session.deviceState
        XCTAssertEqual(cloudState, .removed)
        XCTAssertNil(rig.credentials.load())
        XCTAssertNil(rig.cache.load(.dashboard))
    }

    func testProbingAnUnpairedGateSendsNothing() async {
        let rig = harness(paired: false) { _, _ in .refused(404) }
        await rig.gate.start()
        await rig.gate.probe()
        XCTAssertEqual(rig.gate.phase, .unpaired)
        XCTAssertEqual(rig.transport.requestCount, 0)
    }

    func testForegroundProbeLeavesUnpairedLocalSessionUntouched() async {
        let cloud = harness(paired: false) { _, _ in .refused(404) }
        let transport = LocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: transport)
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session }, makeLocalSession: { local },
            preferences: MemoryPreferenceStore()
        )

        await gate.start()
        await gate.probe()

        XCTAssertTrue(transport.requests.isEmpty)
    }

    func testFreshInstallDefaultsToCloudWhenNeitherBackendIsPaired() async {
        let preference = MemoryPreferenceStore()
        let rig = harness(paired: false) { _, _ in .refused(404) }
        let gate = SessionGate(
            makeSession: { rig.session }, preferences: preference
        )

        await gate.start()

        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertNil(preference.backend)
    }

    func testStartRefreshesBeforeLaunchingNonBlockingAutomaticReevaluation() async {
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let transport = BlockingLocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: transport)
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session }, makeLocalSession: { local },
            preferences: MemoryPreferenceStore()
        )

        let startTask = Task { await gate.start() }
        guard await transport.gate.waitForArrival(timeout: 1) else {
            return XCTFail("automatic local probe did not start")
        }
        await Task.yield()
        XCTAssertEqual(gate.phase, .paired)

        await transport.gate.openGate()
        await startTask.value
    }

    func testPersistedBackendIsAnExplicitOverrideAcrossStart() async {
        let preference = MemoryPreferenceStore(.cloud)
        let cloud = harness(paired: true) { _, _ in .refused(404) }
        let transport = LocalProbeTransport()
        let local = LocalSession(
            credentials: MemoryLocalCredentialStore(credential: try? LocalCredential(
                baseURL: "https://desktop.example", token: "token")),
            cache: MemorySnapshotCache(),
            makeClient: { value in
                try LocalClient(baseURL: URL(string: value.baseURL)!, token: value.token,
                                transport: transport)
            }
        )
        let gate = SessionGate(
            makeSession: { cloud.session }, makeLocalSession: { local },
            preferences: preference
        )

        await gate.start()

        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertEqual(preference.backend, .cloud)
    }

    func testStartingTwiceStartsThePathMonitorOnlyOnce() async {
        let monitor = FakePathMonitor()
        let rig = harness(paired: true) { _, _ in .refused(404) }
        let gate = SessionGate(makeSession: { rig.session }, preferences: MemoryPreferenceStore(), pathMonitor: monitor)

        await gate.start()
        await gate.start()

        XCTAssertEqual(monitor.startCount, 1)
    }

    func testSystemPathMonitorIgnoresDuplicateStartAndCancelIsSafe() {
        let monitor = SystemSessionPathMonitor()
        monitor.start(onChange: {})
        monitor.start(onChange: {})
        monitor.cancel()
        monitor.cancel()
    }
}
