import XCTest

/// The token lifecycle, exercised at the moments it is hard to reason about.
///
/// Every one of these is a failure the rider would otherwise meet as "the app
/// is stuck": a context that died between two screens, five screens refreshing
/// at once, a server asking to be left alone, a phone that was revoked while it
/// was in a pocket, and an hour with no signal.
final class CloudSessionTests: XCTestCase {
    private let baseURL = URL(string: "https://api.example.invalid")!

    private struct Harness {
        let session: CloudSession
        let transport: ScriptedTransport
        let clock: TestClock
        let credentials: MemoryDeviceCredentialStore
        let cache: MemorySnapshotCache
    }

    private func harness(
        paired: Bool = true,
        cache: MemorySnapshotCache = MemorySnapshotCache(),
        clock: TestClock = TestClock(),
        handler: @escaping ScriptedTransport.Handler
    ) -> Harness {
        let transport = ScriptedTransport(handler: handler)
        let credentials = MemoryDeviceCredentialStore(
            device: paired ? CloudFixtures.device : nil
        )
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

    private static func dashboard(_ items: [String], revision: Int) -> CloudResponse {
        .json(CloudFixtures.collection(items: items, revision: revision))
    }

    private static func differentPairingBody(
        context: String, expiresIn: Int = 300
    ) -> String {
        CloudFixtures.pairingBody(context: context)
            .replacingOccurrences(of: "credential-1", with: "credential-2")
            .replacingOccurrences(of: "subscription-1", with: "subscription-2")
            .replacingOccurrences(of: CloudFixtures.namespace, with: String(repeating: "b", count: 64))
            .replacingOccurrences(of: "\"expires_in\":300", with: "\"expires_in\":\(expiresIn)")
    }

    // MARK: - Expiry

    func testATokenNearingExpiryIsReplacedBeforeItIsUsed() async throws {
        let clock = TestClock()
        let rig = harness(paired: false, clock: clock) { request, _ in
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("POST", "/api/v1/devices/pair"):
                return .json(CloudFixtures.pairingBody(context: "context-0"))
            case ("POST", "/api/v1/context/refresh"):
                return .json(CloudFixtures.refreshBody(context: "context-1"))
            default:
                return Self.dashboard(
                    [CloudFixtures.profileItem(revision: 1, ftp: 240)], revision: 1
                )
            }
        }
        try await rig.session.pair(code: "ABCD-EFGH-JKLM")

        // Fresh from pairing: the context is good for five minutes and nothing
        // is signed to read with it.
        _ = try await rig.session.load(.dashboard)
        XCTAssertEqual(rig.transport.requests(matching: "/api/v1/context/refresh").count, 0)
        XCTAssertEqual(rig.transport.requests(matching: "/api/v1/context/dashboard")
            .first?.bearerToken, "context-0")

        // Four minutes later it is inside the refresh-ahead window: still
        // valid, and replaced anyway, so the read does not race the expiry.
        clock.advance(250)
        _ = try await rig.session.load(.dashboard)
        XCTAssertEqual(rig.transport.requests(matching: "/api/v1/context/refresh").count, 1)
        XCTAssertEqual(rig.transport.requests(matching: "/api/v1/context/dashboard")
            .last?.bearerToken, "context-1")
    }

    // MARK: - A rejected context

    /// The read plane answers **404**, not 401, to a rejected reader context:
    /// `_resolve_reader` returning nil becomes `_not_found()` so that nothing
    /// about credential state leaks. A client that waits for a 401 waits
    /// forever, which is why this test asserts on the status the server
    /// actually sends.
    func testARejectedReaderContextForcesOneRefreshAndRetriesThePage() async throws {
        let rig = harness { request, index in
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("POST", "/api/v1/context/refresh"):
                return .json(CloudFixtures.refreshBody(context: "context-\(index)"))
            default:
                // The first read is refused; the retry, with a context minted
                // after the refusal, is served.
                return index == 1
                    ? .refused(404)
                    : Self.dashboard(
                        [CloudFixtures.profileItem(revision: 3, ftp: 250)], revision: 3
                    )
            }
        }

        let snapshot = try await rig.session.load(.dashboard)
        XCTAssertEqual(snapshot.source, .network)
        XCTAssertEqual(snapshot.revision, 3)

        let refreshes = rig.transport.requests(matching: "/api/v1/context/refresh")
        let reads = rig.transport.requests(matching: "/api/v1/context/dashboard")
        XCTAssertEqual(refreshes.count, 2, "the rejection must force exactly one more")
        XCTAssertEqual(reads.count, 2)
        XCTAssertEqual(reads.first?.bearerToken, "context-0")
        XCTAssertEqual(reads.last?.bearerToken, "context-2")
        // A rejected *reader context* is not evidence about the device: only
        // the signed refresh may conclude that.
        let state = await rig.session.deviceState
        XCTAssertEqual(state, .paired)
    }

    // MARK: - Coalescing

    func testConcurrentCallersShareOneRefresh() async throws {
        let gate = RequestGate()
        let rig = harness { request, _ in
            guard request.url?.path == "/api/v1/context/refresh" else {
                return .refused(404)
            }
            // Hold the refresh open so every caller is inside the same window.
            await gate.wait()
            return .json(CloudFixtures.refreshBody(context: "shared-context"))
        }
        let session = rig.session

        async let tokens: [String] = withThrowingTaskGroup(of: String.self) { group in
            for _ in 0..<8 {
                group.addTask { try await session.readerContext() }
            }
            var collected: [String] = []
            for try await value in group { collected.append(value) }
            return collected
        }

        var spins = 0
        while spins < 1_000 {
            if await gate.arrived > 0 { break }
            await Task.yield()
            spins += 1
        }
        await gate.openGate()

        let values = try await tokens
        XCTAssertEqual(values.count, 8)
        XCTAssertEqual(Set(values), ["shared-context"])
        XCTAssertEqual(
            rig.transport.requests(matching: "/api/v1/context/refresh").count, 1,
            "eight callers must not sign eight refreshes"
        )
    }

    // MARK: - Backoff

    func testARetryAfterIsHonouredExactlyAndNothingIsSentInsideIt() async throws {
        let clock = TestClock()
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 4,
                items: [CloudFixtures.item(
                    id: "profile", kind: "profile", revision: 4, data: #"{"ftp":230}"#
                )],
                storedAt: clock.now
            ),
            for: .dashboard
        )
        let rig = harness(cache: cache, clock: clock) { request, index in
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("POST", "/api/v1/context/refresh"):
                return .json(CloudFixtures.refreshBody(context: "context-1"))
            default:
                return index == 1
                    ? .refused(429, retryAfter: 30)
                    : Self.dashboard(
                        [CloudFixtures.profileItem(revision: 5, ftp: 245)], revision: 5
                    )
            }
        }

        let refused = try await rig.session.load(.dashboard)
        XCTAssertEqual(refused.source, .cache, "a 429 shows last-known data")
        XCTAssertEqual(refused.revision, 4)
        XCTAssertEqual(rig.transport.requestCount, 2)

        // Inside the window the app must not touch the network at all.
        let again = try await rig.session.load(.dashboard)
        XCTAssertEqual(again.source, .cache)
        XCTAssertEqual(rig.transport.requestCount, 2, "nothing may be sent inside Retry-After")

        clock.advance(31)
        let recovered = try await rig.session.load(.dashboard)
        XCTAssertEqual(recovered.source, .network)
        XCTAssertEqual(recovered.revision, 5)
        XCTAssertEqual(rig.transport.requestCount, 3)
    }

    // MARK: - Revocation

    func testASecondRefusedRefreshRemovesTheDeviceAndClearsTheCache() async throws {
        let clock = TestClock()
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 9,
                items: [CloudFixtures.item(
                    id: "profile", kind: "profile", revision: 9, data: #"{"ftp":260}"#
                )],
                storedAt: clock.now
            ),
            for: .dashboard
        )
        let rig = harness(cache: cache, clock: clock) { _, _ in
            // The server's clock agrees with the device's, so a skewed clock
            // is ruled out and the refusal can only be about the credential.
            .refused(404, serverDate: clock.now)
        }

        let first = try await rig.session.load(.dashboard)
        XCTAssertEqual(first.source, .cache, "one refusal is not evidence yet")
        var state = await rig.session.deviceState
        XCTAssertEqual(state, .paired)
        XCTAssertNotNil(rig.cache.load(.dashboard))

        clock.advance(60)  // past the backoff the first refusal set
        do {
            _ = try await rig.session.load(.dashboard)
            XCTFail("a twice-refused device must not keep serving the rider's data")
        } catch let failure as CloudSession.Failure {
            guard case .deviceRemoved = failure else {
                return XCTFail("expected removal, got \(failure)")
            }
        }

        state = await rig.session.deviceState
        XCTAssertEqual(state, .removed)
        XCTAssertNil(rig.cache.load(.dashboard), "removal must clear the cache")
        XCTAssertNil(rig.credentials.load(), "and the credential it can no longer use")
        XCTAssertGreaterThanOrEqual(rig.cache.clearCount, 1)

        // And it stays removed without asking the server again.
        let before = rig.transport.requestCount
        do {
            _ = try await rig.session.load(.dashboard)
            XCTFail("a removed device must not retry")
        } catch let failure as CloudSession.Failure {
            guard case .deviceRemoved = failure else {
                return XCTFail("expected removal, got \(failure)")
            }
        }
        XCTAssertEqual(rig.transport.requestCount, before)
    }

    // MARK: - Lifecycle races

    func testARefreshSuspendedAcrossSignOutAndPairCannotPublishTheOldToken() async throws {
        let refreshGate = RequestGate()
        let rig = harness { request, index in
            if index == 0 {
                await refreshGate.wait()
                return .json(CloudFixtures.refreshBody(context: "context-old"))
            }
            if request.url?.path == "/api/v1/devices/pair" {
                return .json(Self.differentPairingBody(context: "context-new"))
            }
            return Self.dashboard(
                [CloudFixtures.profileItem(revision: 1, ftp: 250)], revision: 1
            )
        }

        let oldRead = Task { () -> String in
            do {
                return try await rig.session.readerContext()
            } catch let failure as CloudSession.Failure {
                return failure.description
            } catch {
                return "unexpected error"
            }
        }
        var spins = 0
        while await refreshGate.arrived == 0 && spins < 1_000 {
            await Task.yield()
            spins += 1
        }
        let refreshArrived = await refreshGate.arrived
        XCTAssertEqual(refreshArrived, 1)

        await rig.session.signOut()
        _ = try await rig.session.pair(code: "ABCD-EFGH-JKLM")
        await refreshGate.openGate()

        let oldReadResult = await oldRead.value
        XCTAssertEqual(oldReadResult, "This device is not paired yet")

        let snapshot = try await rig.session.load(.dashboard)
        XCTAssertEqual(snapshot.source, .network)
        let read = try XCTUnwrap(
            rig.transport.requests(matching: "/api/v1/context/dashboard").last
        )
        XCTAssertEqual(read.bearerToken, "context-new")
        XCTAssertEqual(
            read.value(forHTTPHeaderField: "Ocp-Apim-Subscription-Key"),
            "subscription-2"
        )
    }

    func testAnObsoleteRefreshCannotClearAReplacementRefreshTask() async throws {
        let oldGate = RequestGate()
        let replacementGate = RequestGate()
        let rig = harness { request, index in
            if index == 0 {
                await oldGate.wait()
                return .json(CloudFixtures.refreshBody(context: "context-old"))
            }
            if request.url?.path == "/api/v1/devices/pair" {
                return .json(
                    Self.differentPairingBody(context: "context-paired", expiresIn: 10)
                )
            }
            if index == 2 {
                await replacementGate.wait()
                return .json(CloudFixtures.refreshBody(context: "context-replacement"))
            }
            if request.httpMethod == "POST" {
                return .json(CloudFixtures.refreshBody(context: "context-unexpected"))
            }
            return Self.dashboard([], revision: 1)
        }

        let oldRead = Task { () -> String in
            do {
                return try await rig.session.readerContext()
            } catch let failure as CloudSession.Failure {
                return failure.description
            } catch {
                return "unexpected error"
            }
        }
        var spins = 0
        while await oldGate.arrived == 0 && spins < 1_000 {
            await Task.yield()
            spins += 1
        }
        let oldRefreshArrived = await oldGate.arrived
        XCTAssertEqual(oldRefreshArrived, 1)

        await rig.session.signOut()
        _ = try await rig.session.pair(code: "ABCD-EFGH-JKLM")

        let replacement = Task { () -> String in
            try await rig.session.readerContext()
        }
        spins = 0
        while await replacementGate.arrived == 0 && spins < 1_000 {
            await Task.yield()
            spins += 1
        }
        let replacementArrived = await replacementGate.arrived
        XCTAssertEqual(replacementArrived, 1)

        let joiner = Task { () -> String in
            try await rig.session.readerContext()
        }
        for _ in 0..<1_000 { await Task.yield() }

        await oldGate.openGate()
        let oldReadResult = await oldRead.value
        XCTAssertEqual(oldReadResult, "This device is not paired yet")
        let replacementStillWaiting = await replacementGate.arrived
        XCTAssertEqual(replacementStillWaiting, 1)

        await replacementGate.openGate()
        let replacementResult = try await replacement.value
        XCTAssertEqual(replacementResult, "context-replacement")
        let joinerResult = try await joiner.value
        XCTAssertEqual(joinerResult, "context-replacement")
        XCTAssertEqual(
            rig.transport.requests(matching: "/api/v1/context/refresh").count,
            2,
            "the obsolete task must not cause a replacement refresh"
        )
    }

    func testAnInFlightReadCannotReturnOrRestoreDataAfterSigningOut() async throws {
        let gate = RequestGate()
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 4,
                items: [CloudFixtures.item(
                    id: "profile", kind: "profile", revision: 4, data: #"{"ftp":230}"#
                )],
                storedAt: Date()
            ),
            for: .dashboard
        )
        let rig = harness(cache: cache) { request, index in
            if index == 1 {
                await gate.wait()
                return Self.dashboard(
                    [CloudFixtures.profileItem(revision: 5, ftp: 240)], revision: 5
                )
            }
            if request.httpMethod == "POST" {
                return .json(CloudFixtures.refreshBody(context: "context-1"))
            }
            return Self.dashboard([], revision: 5)
        }

        let read = Task { () -> String in
            do {
                _ = try await rig.session.load(.dashboard)
                return "success"
            } catch let failure as CloudSession.Failure {
                return failure.description
            } catch {
                return "unexpected error"
            }
        }
        var spins = 0
        while await gate.arrived == 0 && spins < 1_000 {
            await Task.yield()
            spins += 1
        }
        let arrived = await gate.arrived
        XCTAssertEqual(arrived, 1)

        await rig.session.signOut()
        XCTAssertNil(rig.cache.load(.dashboard))
        await gate.openGate()

        let result = await read.value
        XCTAssertEqual(result, "This device is not paired yet")
        XCTAssertNil(rig.cache.load(.dashboard), "late data must not restore cache")
    }

    func testAnInFlightReadCannotReturnDataAfterPairingADifferentDevice() async throws {
        let gate = RequestGate()
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 4,
                items: [CloudFixtures.item(
                    id: "profile", kind: "profile", revision: 4, data: #"{"ftp":230}"#
                )],
                storedAt: Date()
            ),
            for: .dashboard
        )
        let rig = harness(cache: cache) { request, index in
            if index == 1 {
                await gate.wait()
                return Self.dashboard(
                    [CloudFixtures.profileItem(revision: 5, ftp: 240)], revision: 5
                )
            }
            if request.url?.path == "/api/v1/devices/pair" {
                let body = CloudFixtures.pairingBody(context: "context-new")
                    .replacingOccurrences(of: "credential-1", with: "credential-2")
                    .replacingOccurrences(of: "subscription-1", with: "subscription-2")
                return .json(body)
            }
            if request.httpMethod == "POST" {
                return .json(CloudFixtures.refreshBody(context: "context-1"))
            }
            return Self.dashboard([], revision: 5)
        }

        let read = Task { () -> String in
            do {
                _ = try await rig.session.load(.dashboard)
                return "success"
            } catch let failure as CloudSession.Failure {
                return failure.description
            } catch {
                return "unexpected error"
            }
        }
        var spins = 0
        while await gate.arrived == 0 && spins < 1_000 {
            await Task.yield()
            spins += 1
        }
        let arrived = await gate.arrived
        XCTAssertEqual(arrived, 1)

        _ = try await rig.session.pair(code: "ABCD-EFGH-JKLM")
        XCTAssertNil(rig.cache.load(.dashboard))
        await gate.openGate()

        let result = await read.value
        XCTAssertEqual(result, "This device is not paired yet")
        XCTAssertNil(rig.cache.load(.dashboard), "late data must not restore cache")
    }

    func testAnInFlightReadCannotReturnAfterRePairingTheSameDevice() async throws {
        let gate = RequestGate()
        let rig = harness { request, index in
            if index == 1 {
                await gate.wait()
                return Self.dashboard(
                    [CloudFixtures.profileItem(revision: 5, ftp: 240)], revision: 5
                )
            }
            if request.url?.path == "/api/v1/devices/pair" {
                return .json(CloudFixtures.pairingBody(context: "context-new"))
            }
            if request.httpMethod == "POST" {
                return .json(CloudFixtures.refreshBody(context: "context-old"))
            }
            return Self.dashboard([], revision: 5)
        }

        let read = Task { () -> String in
            do {
                _ = try await rig.session.load(.dashboard)
                return "success"
            } catch let failure as CloudSession.Failure {
                return failure.description
            } catch {
                return "unexpected error"
            }
        }
        var spins = 0
        while await gate.arrived == 0 && spins < 1_000 {
            await Task.yield()
            spins += 1
        }
        let arrived = await gate.arrived
        XCTAssertEqual(arrived, 1)

        await rig.session.signOut()
        _ = try await rig.session.pair(code: "ABCD-EFGH-JKLM")
        XCTAssertNil(rig.cache.load(.dashboard))
        await gate.openGate()

        let result = await read.value
        XCTAssertEqual(result, "This device is not paired yet")
        XCTAssertNil(rig.cache.load(.dashboard), "late data must not restore cache")

        let snapshot = try await rig.session.load(.dashboard)
        XCTAssertEqual(snapshot.source, .network)
        let postPairRead = try XCTUnwrap(
            rig.transport.requests(matching: "/api/v1/context/dashboard").last
        )
        XCTAssertEqual(postPairRead.bearerToken, "context-new")
    }

    func testAnInFlightReadCannotReturnOrRestoreDataAfterRemoval() async throws {
        let clock = TestClock()
        let gate = RequestGate()
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 4,
                items: [CloudFixtures.item(
                    id: "profile", kind: "profile", revision: 4, data: #"{"ftp":230}"#
                )],
                storedAt: clock.now
            ),
            for: .dashboard
        )
        let rig = harness(cache: cache, clock: clock) { request, index in
            if index == 1 {
                await gate.wait()
                return Self.dashboard(
                    [CloudFixtures.profileItem(revision: 5, ftp: 240)], revision: 5
                )
            }
            if request.httpMethod == "POST" {
                return index == 0
                    ? .json(CloudFixtures.refreshBody(context: "context-0"))
                    : .refused(404, serverDate: clock.now)
            }
            if index == 2 || index == 4 {
                return .refused(404, serverDate: clock.now)
            }
            return Self.dashboard([], revision: 5)
        }

        let read = Task { () -> String in
            do {
                _ = try await rig.session.load(.dashboard)
                return "success"
            } catch let failure as CloudSession.Failure {
                return failure.description
            } catch {
                return "unexpected error"
            }
        }
        await gate.waitUntilArrived()
        let arrived = await gate.arrived
        XCTAssertEqual(arrived, 1)

        // The two later refusals remove the device while the original page is
        // still suspended. The original response is deliberately successful.
        _ = try await rig.session.load(.dashboard)
        clock.advance(60)
        do {
            _ = try await rig.session.load(.dashboard)
            XCTFail("the second refused refresh must remove the device")
        } catch let failure as CloudSession.Failure {
            guard case .deviceRemoved = failure else {
                return XCTFail("expected removal, got \(failure)")
            }
        }

        XCTAssertNil(rig.cache.load(.dashboard))
        await gate.openGate()

        let result = await read.value
        XCTAssertEqual(result, "This device was removed")
        XCTAssertNil(rig.cache.load(.dashboard), "late data must not restore cache")
    }

    /// The caller that joined somebody else's refresh must hear the same
    /// answer, not the backoff that refusal also set. It resumes into a session
    /// the refresh has already removed, and "we were removed" is the fact it
    /// has to carry back -- a `throttled` would put the app on a retry timer
    /// for a credential that no longer exists.
    func testACallerJoiningTheRefreshThatRemovesTheDeviceHearsThat() async throws {
        let clock = TestClock()
        let gate = RequestGate()
        let rig = harness(clock: clock) { _, index in
            if index > 0 { await gate.wait() }
            return .refused(404, serverDate: clock.now)
        }
        let session = rig.session

        // One refusal already banked, so the next one is the second.
        _ = try? await session.readerContext()
        clock.advance(60)

        async let outcomes: [String] = withTaskGroup(of: String.self) { group in
            for _ in 0..<2 {
                group.addTask {
                    do {
                        _ = try await session.readerContext()
                        return "unexpected success"
                    } catch let failure as CloudSession.Failure {
                        if case .deviceRemoved = failure { return "removed" }
                        return "\(failure)"
                    } catch {
                        return "\(error)"
                    }
                }
            }
            var collected: [String] = []
            for await value in group { collected.append(value) }
            return collected
        }

        var spins = 0
        while spins < 1_000 {
            if await gate.arrived > 0 { break }
            await Task.yield()
            spins += 1
        }
        await gate.openGate()

        let values = await outcomes
        XCTAssertEqual(values, ["removed", "removed"])
        let state = await session.deviceState
        XCTAssertEqual(state, .removed)
    }

    func testAClockFourMinutesOffTheServerIsNeverMistakenForRevocation() async throws {
        let clock = TestClock()
        let rig = harness(clock: clock) { _, _ in
            // Every signed request from this device is stale by the server's
            // reckoning, so every one of them is refused with the same 404 a
            // revoked device gets.
            .refused(404, serverDate: clock.now.addingTimeInterval(3_600))
        }

        for _ in 0..<3 {
            do {
                _ = try await rig.session.readerContext()
                XCTFail("a device the server thinks is stale cannot get a context")
            } catch let failure as CloudSession.Failure {
                switch failure {
                case .clockSkew, .throttled: break
                default: XCTFail("expected a clock complaint, got \(failure)")
                }
            }
            clock.advance(600)
        }

        let state = await rig.session.deviceState
        XCTAssertEqual(state, .paired, "a wrong clock is not a revoked credential")
        XCTAssertNotNil(rig.credentials.load())
    }

    /// A 404 with no server clock reference at all -- the header was simply
    /// absent -- cannot rule out a skewed device clock any more than a 404
    /// that says the clock is wrong can. Counting it anyway is exactly the
    /// bug: a phone an hour off the server, hitting a 404 that happens to
    /// carry no `Date`, would otherwise reach `removed` on its very next
    /// attempt instead of ever being told about the skew.
    func testAFourOhFourWithNoDateHeaderNeverCountsTowardRemoval() async throws {
        let clock = TestClock()
        let rig = harness(clock: clock) { _, _ in
            .refused(404)  // no serverDate at all
        }

        for _ in 0..<6 {
            do {
                _ = try await rig.session.readerContext()
                XCTFail("a 404 must not mint a context")
            } catch let failure as CloudSession.Failure {
                guard case .server = failure else {
                    return XCTFail(
                        "expected a bare refusal with no clock reference, got \(failure)"
                    )
                }
            }
            clock.advance(600)  // well past any backoff this can set
        }

        let state = await rig.session.deviceState
        XCTAssertEqual(
            state, .paired,
            "a 404 that cannot rule out clock skew must never be banked as a strike"
        )
        XCTAssertNotNil(rig.credentials.load())
    }

    /// `HTTPHeaderDates` reduces both obsolete `Date` spellings RFC 9110 still
    /// requires a recipient tolerate -- RFC 850 and asctime -- to the same
    /// "absent" signal a missing header produces. That is what makes them
    /// covered by the no-`Date` case above rather than needing one of their
    /// own in `CloudSession`: either arrives at `refusal(_:)` as
    /// `serverDate == nil`, indistinguishable from the header never having
    /// been sent.
    func testObsoleteDateHeaderFormatsAreTreatedAsAbsentNotParsed() {
        XCTAssertNil(HTTPHeaderDates.date("Sunday, 06-Nov-94 08:49:37 GMT"), "RFC 850")
        XCTAssertNil(HTTPHeaderDates.date("Sun Nov  6 08:49:37 1994"), "asctime")
        XCTAssertNotNil(
            HTTPHeaderDates.date("Sun, 06 Nov 1994 08:49:37 GMT"),
            "IMF-fixdate is the one format this client does read"
        )
    }

    /// The gap `noteFailure` enforces between the two 404s two-strike removal
    /// counts has to outlast something real, not just an instant -- a rider
    /// pulling to refresh twice, a deployment mid-restart. Two attempts two
    /// seconds apart is exactly what a repeated pull-to-refresh looks like: it
    /// must be throttled, not read as the corroborating second strike.
    func testTwoFourOhFoursTwoSecondsApartAreThrottledNotRemoved() async throws {
        let clock = TestClock()
        let rig = harness(clock: clock) { _, _ in
            .refused(404, serverDate: clock.now)
        }

        do {
            _ = try await rig.session.readerContext()
            XCTFail("a single 404 must not mint a context")
        } catch let failure as CloudSession.Failure {
            guard case .server = failure else {
                return XCTFail("expected a bare refusal, got \(failure)")
            }
        }

        clock.advance(2)  // a rider pulling to refresh again, moments later
        do {
            _ = try await rig.session.readerContext()
            XCTFail("still inside the backoff the first refusal set")
        } catch let failure as CloudSession.Failure {
            guard case .throttled = failure else {
                return XCTFail("expected throttling, not a second strike, got \(failure)")
            }
        }

        XCTAssertEqual(
            rig.transport.requestCount, 1,
            "the throttled attempt must not even reach the server"
        )
        let state = await rig.session.deviceState
        XCTAssertEqual(
            state, .paired, "two attempts two seconds apart must not unpair the device"
        )
        XCTAssertNotNil(rig.credentials.load())
    }

    func testAQuotaRefusalNeverRemovesTheDevice() async throws {
        let clock = TestClock()
        let rig = harness(clock: clock) { _, _ in
            // 403 on the refresh route is a quota refusal, and it is reachable
            // only after the signature already verified.
            .refused(403, retryAfter: 60)
        }

        for _ in 0..<4 {
            do {
                _ = try await rig.session.readerContext()
                XCTFail("a refused refresh cannot produce a context")
            } catch let failure as CloudSession.Failure {
                guard case .throttled = failure else {
                    return XCTFail("expected throttling, got \(failure)")
                }
            }
            clock.advance(120)
        }

        let state = await rig.session.deviceState
        XCTAssertEqual(state, .paired)
        XCTAssertNotNil(rig.credentials.load())
    }

    // MARK: - Cold start and the delta

    func testAColdStartServesTheCacheAndThenReconcilesWithSince() async throws {
        let clock = TestClock()
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 5,
                items: [
                    CloudFixtures.item(
                        id: "profile", kind: "profile", revision: 5, data: #"{"ftp":240}"#
                    ),
                    CloudFixtures.item(
                        id: "training-state", kind: "training_state", revision: 5,
                        data: #"{"ctl":30}"#
                    ),
                ],
                storedAt: clock.now
            ),
            for: .dashboard
        )
        let rig = harness(cache: cache, clock: clock) { request, _ in
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("POST", "/api/v1/context/refresh"):
                return .json(CloudFixtures.refreshBody(context: "context-1"))
            default:
                return .json(CloudFixtures.collection(
                    items: [
                        CloudFixtures.profileItem(revision: 7, ftp: 250),
                        CloudFixtures.tombstone(
                            id: "training-state", kind: "training_state", revision: 7
                        ),
                    ],
                    revision: 7
                ))
            }
        }

        // The screen has data before a single byte moves.
        let cold = try XCTUnwrap(rig.session.cached(.dashboard))
        XCTAssertEqual(cold.source, .cache)
        XCTAssertEqual(cold.revision, 5)
        XCTAssertEqual(cold.items.count, 2)
        XCTAssertEqual(cold.asOf, clock.now, "a cached snapshot carries its real age")
        XCTAssertEqual(rig.transport.requestCount, 0)

        let reconciled = try await rig.session.load(.dashboard)
        XCTAssertEqual(reconciled.source, .network)
        XCTAssertEqual(reconciled.revision, 7)
        XCTAssertEqual(reconciled.items.map(\.id), ["profile"], "the tombstone removes its object")
        if case let .profile(profile) = reconciled.items[0].payload {
            XCTAssertEqual(profile.resolvedFTP, 250)
        } else {
            XCTFail("the surviving object should still decode as a profile")
        }

        let read = try XCTUnwrap(rig.transport.requests(matching: "/api/v1/context/dashboard").first)
        XCTAssertEqual(read.queryItems["since"], "5", "the checkpoint comes from the cache")

        // The next read asks from the checkpoint the server just handed back,
        // never from one this client computed.
        _ = try await rig.session.load(.dashboard)
        let second = try XCTUnwrap(rig.transport.requests(matching: "/api/v1/context/dashboard").last)
        XCTAssertEqual(second.queryItems["since"], "7")
    }

    func testARouteThatServesNoDeltaIsNeverAskedForOne() async throws {
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 3,
                items: [CloudFixtures.item(
                    id: "activity-1", kind: "activity", revision: 3, data: #"{"tss":80}"#
                )],
                storedAt: Date()
            ),
            for: .activities
        )
        let rig = harness(cache: cache) { request, _ in
            request.url?.path == "/api/v1/context/refresh"
                ? .json(CloudFixtures.refreshBody(context: "context-1"))
                : .json(#"{"items":[]}"#)
        }

        let snapshot = try await rig.session.load(.activities)
        let read = try XCTUnwrap(rig.transport.requests(matching: "/api/v1/context/activities").first)
        XCTAssertNil(read.queryItems["since"], "activities is not a mobile delta route")
        XCTAssertTrue(snapshot.items.isEmpty, "a full read replaces rather than merges")
    }

    // MARK: - Offline

    func testAnHourInAirplaneModeServesTheCacheAndThenRecovers() async throws {
        let clock = TestClock()
        let start = clock.now
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(
                revision: 11,
                items: [CloudFixtures.item(
                    id: "profile", kind: "profile", revision: 11, data: #"{"ftp":215}"#
                )],
                storedAt: start
            ),
            for: .dashboard
        )
        let rig = harness(cache: cache, clock: clock) { request, _ in
            guard clock.now.timeIntervalSince(start) >= 3_600 else {
                throw URLError(.notConnectedToInternet)
            }
            return request.url?.path == "/api/v1/context/refresh"
                ? .json(CloudFixtures.refreshBody(context: "context-1"))
                : Self.dashboard(
                    [CloudFixtures.profileItem(revision: 12, ftp: 220)], revision: 12
                )
        }

        for minute in stride(from: 0, to: 60, by: 10) {
            let snapshot = try await rig.session.load(.dashboard)
            XCTAssertEqual(snapshot.source, .cache, "minute \(minute)")
            XCTAssertEqual(snapshot.revision, 11)
            // An hour on, the screen can still say how old this is rather than
            // presenting it as current.
            XCTAssertEqual(snapshot.asOf, start)
            clock.advance(600)
        }
        let state = await rig.session.deviceState
        XCTAssertEqual(state, .paired, "no signal is not a revoked credential")

        let recovered = try await rig.session.load(.dashboard)
        XCTAssertEqual(recovered.source, .network)
        XCTAssertEqual(recovered.revision, 12)
    }

    // MARK: - Pairing and signing out

    func testPairingStartsFromNothingAndSigningOutLeavesNothing() async throws {
        let cache = MemorySnapshotCache()
        cache.store(
            CachedCollection(revision: 2, items: [], storedAt: Date()), for: .dashboard
        )
        let rig = harness(paired: false, cache: cache) { _, _ in
            .json(CloudFixtures.pairingBody(context: "context-0"))
        }

        let device = try await rig.session.pair(code: "ABCD-EFGH-JKLM")
        XCTAssertEqual(device.credentialID, "credential-1")
        XCTAssertEqual(rig.credentials.load(), device)
        XCTAssertNil(
            rig.cache.load(.dashboard),
            "a new credential's scope has nothing to do with the old one's revisions"
        )

        await rig.session.signOut()
        XCTAssertNil(rig.credentials.load())
        let state = await rig.session.deviceState
        XCTAssertEqual(state, .unpaired)
    }

    func testAnUnpairedSessionAsksForNothing() async throws {
        let rig = harness(paired: false) { _, _ in .refused(404) }
        do {
            _ = try await rig.session.readerContext()
            XCTFail("there is no credential to sign with")
        } catch let failure as CloudSession.Failure {
            guard case .notPaired = failure else {
                return XCTFail("expected notPaired, got \(failure)")
            }
        }
        XCTAssertEqual(rig.transport.requestCount, 0)
    }
}
