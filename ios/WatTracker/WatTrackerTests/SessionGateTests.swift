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
        private(set) var savedBackends: [SessionGate.Backend] = []

        init(_ backend: SessionGate.Backend? = nil) {
            self.backend = backend
        }

        func loadBackend() -> SessionGate.Backend? { backend }
        func saveBackend(_ backend: SessionGate.Backend) {
            self.backend = backend
            savedBackends.append(backend)
        }
    }

    private final class FakePathMonitor: SessionPathMonitor, @unchecked Sendable {
        private var handler: (@Sendable () -> Void)?
        func start(onChange: @escaping @Sendable () -> Void) { handler = onChange }
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

    func testColdLaunchPrefersReachableLocalOverPersistedCloudPreference() async {
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

        XCTAssertEqual(gate.backend, .local)
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
        let gate = SessionGate(makeSession: { cloud.session }, makeLocalSession: { local },
                               preferences: MemoryPreferenceStore(), pathMonitor: monitor)
        await gate.start()
        await gate.selectBackend(.cloud)
        transport.setReachable(true)
        monitor.trigger()
        await Task.yield()
        XCTAssertEqual(gate.backend, .cloud)
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
        await gate.selectBackend(.cloud)
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
        XCTAssertEqual(preference.loadBackend(), .local)
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
        XCTAssertEqual(preference.backend, .cloud)
        XCTAssertNil(gate.lastSuccess)

        let model = PairingModel()
        model.appeared(gate: gate)
        XCTAssertEqual(model.backend, .local)
        await model.selectBackend(.cloud, gate: gate)

        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertEqual(gate.phase, .paired)
        XCTAssertEqual(preference.backend, .cloud)
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

        await gate.selectBackend(.local)
        try? await gate.removeDevice()

        XCTAssertEqual(gate.backend, .cloud)
        XCTAssertEqual(gate.phase, .paired)
        XCTAssertEqual(preference.backend, .cloud)
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

    func testAServerSideRevokeReachesTheGateOnAProbe() async {
        let clock = TestClock()
        let rig = harness(paired: true, clock: clock) { _, _ in
            // Every authentication failure on this plane is a 404; the `Date`
            // header is what lets the session rule its own clock out.
            .refused(404, serverDate: clock.now)
        }
        await rig.gate.start()

        // One refusal is also what a deployment mid-restart looks like, so it
        // must not be enough.
        await rig.gate.probe()
        XCTAssertEqual(rig.gate.phase, .paired)

        // Past the backoff the session enforces between the two strikes.
        clock.advance(400)
        await rig.gate.probe()
        XCTAssertEqual(rig.gate.phase, .removed)
        XCTAssertNil(rig.credentials.load())

        // And there is a way back: the removed screen's only action.
        await rig.gate.startOver()
        XCTAssertEqual(rig.gate.phase, .unpaired)
    }

    func testProbingAnUnpairedGateSendsNothing() async {
        let rig = harness(paired: false) { _, _ in .refused(404) }
        await rig.gate.start()
        await rig.gate.probe()
        XCTAssertEqual(rig.gate.phase, .unpaired)
        XCTAssertEqual(rig.transport.requestCount, 0)
    }
}
