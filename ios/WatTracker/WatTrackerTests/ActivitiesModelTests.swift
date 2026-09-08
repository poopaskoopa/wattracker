import XCTest

/// What the activities list is allowed to keep on screen.
///
/// The revocation path is the one that matters here, and it is the one that
/// used to be wrong: a rider sitting on the Activities tab pulls to refresh,
/// the desktop has already revoked the device, and the second refused read
/// removes it inside the session -- credential, cache and activity objects all
/// gone. `AppGate`'s scene-phase probe never runs, because the app was never
/// backgrounded, so nothing else re-reads the gate. If the screen keeps its
/// last snapshot, the revoked rider's whole ride list stays rendered under an
/// error line. These check that it does not.
@MainActor
final class ActivitiesModelTests: XCTestCase {
    private let baseURL = URL(string: "https://api.example.invalid")!

    private struct Harness {
        let session: CloudSession
        let transport: ScriptedTransport
        let clock: TestClock
        let credentials: MemoryDeviceCredentialStore
        let cache: MemorySnapshotCache
    }

    /// A server that serves one ride until `revoke()`, and refuses everything
    /// afterwards the way a revoked credential is refused: **404**, with a
    /// `Date` that agrees with the device's clock so skew is ruled out.
    private actor Server {
        private var revoked = false
        func revoke() { revoked = true }
        var isRevoked: Bool { revoked }
    }

    private func harness(
        paired: Bool = true,
        clock: TestClock = TestClock(),
        handler: @escaping ScriptedTransport.Handler
    ) -> Harness {
        let transport = ScriptedTransport(handler: handler)
        let credentials = MemoryDeviceCredentialStore(
            device: paired ? CloudFixtures.device : nil
        )
        let cache = MemorySnapshotCache()
        let client = CloudClient(
            baseURL: baseURL,
            signer: StubSigner(),
            transport: transport,
            clock: clock.reader
        )
        return Harness(
            session: CloudSession(
                client: client, credentials: credentials, cache: cache,
                clock: clock.reader
            ),
            transport: transport,
            clock: clock,
            credentials: credentials,
            cache: cache
        )
    }

    private nonisolated static func activityItem(id: String, startTime: String) -> String {
        """
        {"id":"\(id)","kind":"activity","revision":1,"data":{\
        "start_time":"\(startTime)","duration_s":3600,"distance_m":32100.5,\
        "avg_power":211,"np":225,"if_":0.9,"tss":81,"rpe":6}}
        """
    }

    private func servingSession(_ server: Server, clock: TestClock) -> Harness {
        harness(clock: clock) { request, _ in
            if await server.isRevoked { return .refused(404, serverDate: clock.now) }
            if request.url?.path == "/api/v1/context/refresh" {
                return .json(CloudFixtures.refreshBody(context: "context-1"))
            }
            return .json(CloudFixtures.collection(
                items: [
                    Self.activityItem(id: "activity-17", startTime: "2026-01-02T18:04:00Z"),
                    Self.activityItem(id: "activity-18", startTime: "2026-01-03T18:04:00Z"),
                ],
                revision: 4
            ))
        }
    }

    func testARevokedDeviceLosesTheRenderedRideList() async throws {
        let clock = TestClock()
        let server = Server()
        let rig = servingSession(server, clock: clock)
        let model = ActivitiesModel()

        await model.refresh(session: rig.session)
        XCTAssertEqual(model.rides.count, 2, "the paired rider sees their rides")
        XCTAssertEqual(model.availability, .available)
        XCTAssertNotNil(model.selectedID)

        // The desktop revokes the device. One refusal is not evidence, so the
        // list is still the rider's own cached data and stays on screen.
        await server.revoke()
        clock.advance(60)
        await model.refresh(session: rig.session)
        XCTAssertEqual(model.rides.count, 2, "one refusal is not a revocation")
        XCTAssertEqual(model.availability, .available)

        // The second refusal removes the device inside the session. Nothing
        // backgrounds the app, so this refresh is the only thing that can take
        // the rides off the screen.
        clock.advance(60)
        await model.refresh(session: rig.session)

        let state = await rig.session.deviceState
        XCTAssertEqual(state, .removed)
        XCTAssertEqual(model.availability, .removed)
        XCTAssertTrue(model.rides.isEmpty, "a revoked rider's rides must not stay rendered")
        XCTAssertNil(model.snapshot)
        XCTAssertNil(model.selectedID)
    }

    func testAnUnpairedSessionLosesTheRenderedRideList() async throws {
        let clock = TestClock()
        let rig = servingSession(Server(), clock: clock)
        let model = ActivitiesModel()

        await model.refresh(session: rig.session)
        XCTAssertEqual(model.rides.count, 2)

        // Signing out at the desktop leaves the gate with an unpaired session.
        let unpaired = harness(paired: false, clock: clock) { _, _ in
            XCTFail("an unpaired device must not be asked to read")
            return .refused(404)
        }
        await model.refresh(session: unpaired.session)

        XCTAssertEqual(model.availability, .unpaired)
        XCTAssertTrue(model.rides.isEmpty, "a signed-out rider's rides must not stay rendered")
        XCTAssertNil(model.snapshot)
        XCTAssertNil(model.selectedID)
        XCTAssertEqual(unpaired.transport.requestCount, 0)
    }

    func testNoSessionLosesTheRenderedRideList() async throws {
        let clock = TestClock()
        let rig = servingSession(Server(), clock: clock)
        let model = ActivitiesModel()

        await model.refresh(session: rig.session)
        XCTAssertEqual(model.rides.count, 2)

        // The gate can drop the session out from under this screen; with none
        // there is nothing that could refresh what is on it.
        await model.refresh(session: nil)

        XCTAssertEqual(model.availability, .unpaired)
        XCTAssertTrue(model.rides.isEmpty)
        XCTAssertNil(model.snapshot)
    }

    func testAnOrdinaryNetworkFailureKeepsTheRidesVisible() async throws {
        let clock = TestClock()
        let server = Server()
        let rig = servingSession(server, clock: clock)
        let model = ActivitiesModel()

        await model.refresh(session: rig.session)
        XCTAssertEqual(model.rides.count, 2)

        // A single refusal is served from cache: the rider keeps their list.
        await server.revoke()
        clock.advance(60)
        await model.refresh(session: rig.session)

        XCTAssertEqual(model.availability, .available)
        XCTAssertEqual(model.snapshot?.source, .cache)
        XCTAssertEqual(model.rides.count, 2)
    }
}
