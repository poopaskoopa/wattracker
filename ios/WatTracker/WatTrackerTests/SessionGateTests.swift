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
        let transport: ScriptedTransport
        let clock: TestClock
        let credentials: MemoryDeviceCredentialStore
        let cache: MemorySnapshotCache
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
            gate: SessionGate(makeSession: { session }),
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

    func testAKeyThatCannotBeCreatedIsUnusable() async {
        struct NoKey: Error {}
        let gate = SessionGate(makeSession: { throw NoKey() })
        await gate.start()
        guard case .unusable = gate.phase else {
            return XCTFail("expected .unusable, got \(gate.phase)")
        }
        XCTAssertNil(gate.session)
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
