import Foundation
import SwiftUI
import XCTest

private struct TestReadSessionKey: EnvironmentKey {
    static let defaultValue: (any ReadSession)? = nil
}

extension EnvironmentValues {
    var readSession: (any ReadSession)? {
        get { self[TestReadSessionKey.self] }
        set { self[TestReadSessionKey.self] = newValue }
    }
}

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

private actor RefreshRecordingSession: ReadSession {
    private(set) var retryCallCount = 0

    var deviceState: CloudSession.DeviceState { .paired }
    var lastSuccess: Date? { nil }

    nonisolated func cached(_ route: CloudRoute) -> CloudSnapshot? { nil }

    func load(_ route: CloudRoute) async throws -> CloudSnapshot {
        CloudSnapshot(
            route: route,
            revision: 1,
            items: [],
            source: .network,
            asOf: Date()
        )
    }

    func activityDetail(_ activityID: Int) async throws -> ActivityDetail {
        throw UnexpectedRead.activityDetail
    }

    func activityStreams(_ activityID: Int) async throws -> ActivityStreams {
        throw UnexpectedRead.activityStreams
    }

    func retryNowIfWaking() async {
        retryCallCount += 1
    }

    func retryCalls() -> Int { retryCallCount }

    private enum UnexpectedRead: Error {
        case activityDetail
        case activityStreams
    }
}

@MainActor
final class PullToRefreshModelTests: XCTestCase {
    func testActivitiesRefreshOnlyLiftsWakingGateWhenUserInitiated() async {
        let session = RefreshRecordingSession()
        let model = ActivitiesModel()

        await model.refresh(session: session)
        let initialCalls = await session.retryCalls()
        XCTAssertEqual(initialCalls, 0)

        await model.refresh(session: session, userInitiated: true)
        let refreshedCalls = await session.retryCalls()
        XCTAssertEqual(refreshedCalls, 1)
    }

    func testDashboardStartOnlyLiftsWakingGateWhenUserInitiated() async {
        let session = RefreshRecordingSession()
        let model = DashboardModel()

        await model.start(session: session)
        let initialCalls = await session.retryCalls()
        XCTAssertEqual(initialCalls, 0)

        await model.start(session: session, userInitiated: true)
        let refreshedCalls = await session.retryCalls()
        XCTAssertEqual(refreshedCalls, 1)
    }

    func testCalendarStartOnlyLiftsWakingGateWhenUserInitiated() async {
        let session = RefreshRecordingSession()
        let model = CalendarModel()

        await model.start(session: session)
        let initialCalls = await session.retryCalls()
        XCTAssertEqual(initialCalls, 0)

        await model.start(session: session, userInitiated: true)
        let refreshedCalls = await session.retryCalls()
        XCTAssertEqual(refreshedCalls, 1)
    }

    func testVolumeStartOnlyLiftsWakingGateWhenUserInitiated() async {
        let session = RefreshRecordingSession()
        let model = VolumeScreenModel()

        await model.start(session: session)
        let initialCalls = await session.retryCalls()
        XCTAssertEqual(initialCalls, 0)

        await model.start(session: session, userInitiated: true)
        let refreshedCalls = await session.retryCalls()
        XCTAssertEqual(refreshedCalls, 1)
    }
}

/// The dashboard's two Android-parity additions (#475 B5): TSS bars on the
/// load chart and the Recent rides strip.
@MainActor
final class DashboardTssAndRecentRidesTests: XCTestCase {
    private func ride(_ id: Int, _ start: String?, np: Double? = nil, tss: Double? = nil) -> CloudItem {
        CloudItem(
            id: "activity-\(id)", kind: .activity, revision: 1,
            payload: .activity(ActivitySummary(
                id: Double(id), startTime: start, durationS: 3_600,
                distanceM: 30_000, avgPower: nil, avgHr: nil, np: np,
                intensityFactor: nil, tss: tss, rpe: nil
            ))
        )
    }

    private func snapshot(_ route: CloudRoute, _ items: [CloudItem]) -> CloudSnapshot {
        CloudSnapshot(route: route, revision: 1, items: items, source: .network, asOf: Date())
    }

    private actor Session: ReadSession {
        let activities: CloudSnapshot
        let failActivities: Bool
        init(activities: CloudSnapshot, failActivities: Bool = false) {
            self.activities = activities
            self.failActivities = failActivities
        }
        var deviceState: CloudSession.DeviceState { .paired }
        var lastSuccess: Date? { nil }
        nonisolated func cached(_ route: CloudRoute) -> CloudSnapshot? { nil }
        func load(_ route: CloudRoute) async throws -> CloudSnapshot {
            if route == .activities {
                if failActivities { throw CloudSession.Failure.offline }
                return activities
            }
            return CloudSnapshot(
                route: route, revision: 1,
                items: [CloudItem(id: "lp", kind: .loadPoint, revision: 1,
                                  payload: .loadPoint(LoadPoint(date: "2026-11-01", tss: 80, ctl: 50, atl: 60, tsb: -10)))],
                source: .network, asOf: Date()
            )
        }
        func activityDetail(_ activityID: Int) async throws -> ActivityDetail { throw CloudSession.Failure.offline }
        func activityStreams(_ activityID: Int) async throws -> ActivityStreams { throw CloudSession.Failure.offline }
    }

    func testTssBarsKeepOnlyPositiveFiniteDatedDays() {
        let points = [
            LoadPoint(date: "2026-11-01", tss: 80, ctl: nil, atl: nil, tsb: nil),
            LoadPoint(date: "2026-11-02", tss: 0, ctl: 1, atl: 1, tsb: 1),
            LoadPoint(date: "2026-11-03", tss: -5, ctl: nil, atl: nil, tsb: nil),
            LoadPoint(date: "2026-11-04", tss: nil, ctl: 1, atl: 1, tsb: 1),
            LoadPoint(date: "2026-11-05", tss: .infinity, ctl: nil, atl: nil, tsb: nil),
            LoadPoint(date: nil, tss: 40, ctl: nil, atl: nil, tsb: nil),
            LoadPoint(date: "2026-11-06", tss: 120.5, ctl: nil, atl: nil, tsb: nil),
        ]
        let bars = DashboardData.tssBars(points)
        XCTAssertEqual(bars.map(\.tss), [80, 120.5])
    }

    func testRecentRidesAreNewestFirstAndCappedAtFive() {
        let items = (1...7).map { ride($0, String(format: "2026-11-%02dT08:00:00Z", $0)) }
            + [CloudItem(id: "activity-99", kind: .activity, revision: 2, deleted: true,
                         payload: .activity(ActivitySummary(
                            id: 99, startTime: "2026-12-01T08:00:00Z", durationS: nil, distanceM: nil,
                            avgPower: nil, avgHr: nil, np: nil, intensityFactor: nil, tss: nil, rpe: nil)))]
        let rides = DashboardModel.recentRides(from: snapshot(.activities, items))
        XCTAssertEqual(rides.map(\.activityID), [7, 6, 5, 4, 3])
    }

    func testRowUsesParsedDateNotARawSlice() {
        let line = RecentRideLine(ActivitySummary(
            id: 1, startTime: "2026-11-03T14:30:00Z", durationS: 3_725, distanceM: 40_250,
            avgPower: nil, avgHr: nil, np: 212.4, intensityFactor: nil, tss: 71.6, rpe: nil
        ))
        XCTAssertEqual(line.date, RideFormatting.date("2026-11-03T14:30:00Z"))
        XCTAssertNotEqual(line.date, "2026-11-03")
        XCTAssertEqual(line.detail, "1:02:05 · 40.2 km")
        XCTAssertEqual(line.load, "212 W NP · 72 TSS")
    }

    func testRowOmitsMissingLoadFigures() {
        let line = RecentRideLine(ActivitySummary(
            id: 1, startTime: nil, durationS: nil, distanceM: nil,
            avgPower: nil, avgHr: nil, np: nil, intensityFactor: nil, tss: nil, rpe: nil
        ))
        XCTAssertEqual(line.date, "Unknown date")
        XCTAssertEqual(line.load, "")
    }

    func testModelFillsStripFromActivitiesRouteAfterDashboardLoads() async {
        let session = Session(activities: snapshot(.activities, [
            ride(1, "2026-11-01T08:00:00Z"), ride(2, "2026-11-02T08:00:00Z"),
        ]))
        let model = DashboardModel()
        await model.start(session: session)
        XCTAssertTrue(model.isReady)
        XCTAssertEqual(model.recentRides.map(\.activityID), [2, 1])
    }

    func testActivitiesFailureLeavesDashboardReadyWithoutStrip() async {
        let session = Session(activities: snapshot(.activities, []), failActivities: true)
        let model = DashboardModel()
        await model.start(session: session)
        XCTAssertTrue(model.isReady)
        XCTAssertTrue(model.recentRides.isEmpty)
    }

    // MARK: - The strip belongs to one read session (PR #480 review)

    /// The dashboard metrics every scripted session returns.
    nonisolated private static func dashboardSnapshot(_ route: CloudRoute) -> CloudSnapshot {
        CloudSnapshot(
            route: route, revision: 1,
            items: [CloudItem(id: "lp", kind: .loadPoint, revision: 1,
                              payload: .loadPoint(LoadPoint(date: "2026-11-01", tss: 80, ctl: 50, atl: 60, tsb: -10)))],
            source: .network, asOf: Date()
        )
    }

    /// A paired session whose activities replies are scripted in order (the
    /// last one repeats), and which can hold its first reply until released.
    private actor ScriptedSession: ReadSession {
        enum Reply: Sendable {
            case rides(CloudSnapshot)
            case fail(CloudSession.Failure)
        }

        private let cachedActivities: CloudSnapshot?
        private var replies: [Reply]
        private var holding: Bool
        private var release: CheckedContinuation<Void, Never>?
        private(set) var activitiesRequested = false

        init(cachedActivities: CloudSnapshot? = nil, replies: [Reply], hold: Bool = false) {
            self.cachedActivities = cachedActivities
            self.replies = replies
            self.holding = hold
        }

        var deviceState: CloudSession.DeviceState { .paired }
        var lastSuccess: Date? { nil }
        nonisolated func cached(_ route: CloudRoute) -> CloudSnapshot? {
            route == .activities ? cachedActivities : nil
        }

        func load(_ route: CloudRoute) async throws -> CloudSnapshot {
            guard route == .activities else { return DashboardTssAndRecentRidesTests.dashboardSnapshot(route) }
            let reply = replies.count > 1 ? replies.removeFirst() : replies[0]
            activitiesRequested = true
            if holding {
                await withCheckedContinuation { release = $0 }
            }
            switch reply {
            case let .rides(snapshot): return snapshot
            case let .fail(failure): throw failure
            }
        }

        func releaseActivities() {
            holding = false
            release?.resume()
            release = nil
        }

        func activityDetail(_ activityID: Int) async throws -> ActivityDetail { throw CloudSession.Failure.offline }
        func activityStreams(_ activityID: Int) async throws -> ActivityStreams { throw CloudSession.Failure.offline }
    }

    private func rides(_ ids: Int...) -> CloudSnapshot {
        snapshot(.activities, ids.map { ride($0, String(format: "2026-11-%02dT08:00:00Z", $0)) })
    }

    private func waitUntilActivitiesRequested(_ session: ScriptedSession) async {
        while !(await session.activitiesRequested) { await Task.yield() }
    }

    /// The review's reproduction: B's metrics must never be shown beside A's
    /// rides when B has no activities cache and its activities read fails.
    func testNewSessionWithoutCacheAndFailedActivitiesShowsNoPreviousSessionRides() async {
        let model = DashboardModel()
        await model.start(session: ScriptedSession(replies: [.rides(rides(1, 2))]))
        XCTAssertEqual(model.recentRides.map(\.activityID), [2, 1])

        await model.start(session: ScriptedSession(replies: [.fail(.offline)]))
        XCTAssertTrue(model.isReady)
        XCTAssertEqual(model.recentRides.map(\.activityID), [])
    }

    func testSameSessionRefreshKeepsItsRidesWhenActivitiesFail() async {
        let model = DashboardModel()
        let session = ScriptedSession(replies: [.rides(rides(1, 2)), .fail(.offline)])
        await model.start(session: session)
        XCTAssertEqual(model.recentRides.map(\.activityID), [2, 1])

        await model.start(session: session, userInitiated: true)
        XCTAssertTrue(model.isReady)
        XCTAssertEqual(model.recentRides.map(\.activityID), [2, 1])
    }

    func testNewSessionCachedRidesReplacePreviousOnesBeforeTheNetworkAnswers() async {
        let model = DashboardModel()
        await model.start(session: ScriptedSession(replies: [.rides(rides(1, 2))]))

        let next = ScriptedSession(cachedActivities: rides(7, 8), replies: [.rides(rides(9))], hold: true)
        let task = Task { await model.start(session: next) }
        await waitUntilActivitiesRequested(next)
        XCTAssertTrue(model.isReady)
        XCTAssertEqual(model.recentRides.map(\.activityID), [8, 7])

        await next.releaseActivities()
        await task.value
        XCTAssertEqual(model.recentRides.map(\.activityID), [9])
    }

    func testSuccessfulEmptyActivitiesReplyClearsTheStrip() async {
        let model = DashboardModel()
        let session = ScriptedSession(replies: [.rides(rides(1, 2)), .rides(rides())])
        await model.start(session: session)
        XCTAssertEqual(model.recentRides.map(\.activityID), [2, 1])

        await model.start(session: session, userInitiated: true)
        XCTAssertTrue(model.isReady)
        XCTAssertEqual(model.recentRides.map(\.activityID), [])
    }

    func testDelayedPreviousSessionActivitiesCannotOverwriteTheNewSession() async {
        let model = DashboardModel()
        let old = ScriptedSession(replies: [.rides(rides(1, 2))], hold: true)
        let oldTask = Task { await model.start(session: old) }
        await waitUntilActivitiesRequested(old)

        await model.start(session: ScriptedSession(replies: [.rides(rides(5))]))
        XCTAssertEqual(model.recentRides.map(\.activityID), [5])

        await old.releaseActivities()
        await oldTask.value
        XCTAssertTrue(model.isReady)
        XCTAssertEqual(model.recentRides.map(\.activityID), [5])
    }

    func testDelayedPreviousSessionTerminalFailureCannotEndTheNewSessionsScreen() async {
        let model = DashboardModel()
        let old = ScriptedSession(replies: [.fail(.deviceRemoved)], hold: true)
        let oldTask = Task { await model.start(session: old) }
        await waitUntilActivitiesRequested(old)

        await model.start(session: ScriptedSession(replies: [.rides(rides(5))]))
        await old.releaseActivities()
        await oldTask.value
        XCTAssertTrue(model.isReady)
        XCTAssertEqual(model.recentRides.map(\.activityID), [5])
    }

    func testTerminalActivitiesFailuresClearTheStripAndEndThePairedScreen() async {
        for (failure, expectRemoved) in [(CloudSession.Failure.notPaired, false), (.deviceRemoved, true)] {
            let model = DashboardModel()
            let session = ScriptedSession(replies: [.rides(rides(1, 2)), .fail(failure)])
            await model.start(session: session)
            XCTAssertEqual(model.recentRides.map(\.activityID), [2, 1])

            await model.start(session: session, userInitiated: true)
            XCTAssertTrue(model.recentRides.isEmpty)
            switch model.state {
            case .removed: XCTAssertTrue(expectRemoved)
            case .unpaired: XCTAssertFalse(expectRemoved)
            default: XCTFail("expected a terminal state, got \(model.state)")
            }
        }
    }
}
