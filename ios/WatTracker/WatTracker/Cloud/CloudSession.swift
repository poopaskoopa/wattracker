import Foundation

/// The read-plane routes, and which of them serve deltas.
///
/// `api.py` marks `dashboard`, `volume` and `curve` `mobile=True`; only those
/// three read `since=`, return a `revision`, page with a signed `cursor`, and
/// include tombstones.  The rest answer `{"items": [...]}` and nothing else,
/// so there is no checkpoint to cache against and asking for one would be
/// asking a question the route does not answer.  That is a fact about the
/// route, kept here, rather than something inferred from a response -- an
/// absent `revision` also describes a truncated body.
enum CloudRoute: String, Sendable, Equatable, CaseIterable {
    case dashboard
    case volume
    case curve
    case profile
    case activities
    case calendar
    case races

    var path: String { "/api/v1/context/\(rawValue)" }

    var servesDeltas: Bool {
        switch self {
        case .dashboard, .volume, .curve: return true
        case .profile, .activities, .calendar, .races: return false
        }
    }

    /// The cache file's name.  A fixed alphabet by construction: these are the
    /// enum's own cases and never anything a server or a rider chose.
    var cacheKey: String { rawValue }
}

/// A route's objects as the app should render them right now.
struct CloudSnapshot: Sendable, Equatable {
    enum Source: Sendable, Equatable {
        case network
        /// The network attempt failed or was refused; this is last-known data.
        case cache
    }

    let route: CloudRoute
    let revision: Int
    let items: [CloudItem]
    let source: Source
    /// When these objects were read from the server.
    ///
    /// Carried rather than inferred because a screen showing `.cache` has to be
    /// able to say how old it is. Stale data with no age on it is the trap this
    /// avoids: the rider cannot tell yesterday's numbers from this morning's.
    let asOf: Date
}

/// The token lifecycle, the offline cache, and the one place that decides this
/// device is gone.
///
/// An actor rather than a lock because the interesting states are all
/// concurrent ones: five screens asking for data at launch, a token that
/// expires between two of them, a refresh that must happen once for all five.
/// Actor reentrancy is what makes the coalescing work -- callers that arrive
/// while a refresh is suspended see the in-flight `Task` and await *it*
/// instead of starting a second signed refresh.
actor CloudSession {
    /// `READER_CONTEXT_TTL_SECONDS` in `security.py`.  Used only where the
    /// server did not say; the response's own `expires_in` always wins.
    static let defaultContextLifetime: TimeInterval = 300
    /// Refresh this far ahead of expiry.  A fifth of the lifetime: long enough
    /// that a slow refresh on a poor connection still lands before the token
    /// it replaces dies, short enough that most refreshes are not wasted work.
    static let refreshAhead: TimeInterval = 60
    /// A token with less than this left is not handed out at all. Without it a
    /// caller can be given a token that expires while its own request is in
    /// flight, which becomes a 404 and a retry for no reason.
    static let minimumUsableLifetime: TimeInterval = 15
    /// A token is never trusted for longer than the server's own TTL even if a
    /// response claims more: `expires_in` is the server describing its policy,
    /// not granting an extension this client may award itself.
    static let maximumContextLifetime: TimeInterval = 300
    /// `_MAX_TIMESTAMP` in `api.py` is 300 seconds either way. Past 240 the
    /// device's own clock is the likeliest reason a signed request is refused,
    /// and telling the rider their device was removed would be a lie.
    static let clockSkewTolerance: TimeInterval = 240
    /// Backoff bounds for a refusal the server put no `Retry-After` on. The
    /// floor is not decorative: it is also the shortest gap `noteFailure` can
    /// ever leave between the two 404s two-strike removal counts, so it has
    /// to be wide enough that a deployment restart -- the false positive this
    /// scheme exists to absorb -- has plausibly finished before the second
    /// attempt is allowed. A couple of seconds, which is what this used to be,
    /// outlasts nothing.
    static let baseBackoff: TimeInterval = 30
    static let maximumBackoff: TimeInterval = 300
    /// A signed refresh must be refused twice, with a backoff between, before
    /// the device declares itself removed. One 404 is also what a deployment
    /// mid-restart produces, and the action taken on removal -- wiping the
    /// cache and the credential -- is not one to take on a single sample.
    /// That protection is only as real as the gap enforced between the two
    /// attempts; see `baseBackoff`.
    static let rejectionsBeforeRemoval = 2
    /// Cursor pages per collection read. Far above any real scope, and here so
    /// that a server answering with a cursor that never terminates costs a
    /// bounded number of requests rather than a loop.
    static let maximumPages = 50

    /// Where this device stands with the server.
    enum DeviceState: Sendable, Equatable {
        case unpaired
        case paired
        /// The server has refused this device's signed refresh, repeatedly,
        /// with the device's own clock ruled out. The credential and the cache
        /// are already gone by the time this is observable.
        case removed
    }

    enum Failure: Error, CustomStringConvertible {
        case notPaired
        case deviceRemoved
        case throttled(retryAfter: TimeInterval)
        case clockSkew(seconds: TimeInterval)
        case offline
        case server(CloudClient.Failure)

        var description: String {
            switch self {
            case .notPaired:
                return "This device is not paired yet"
            case .deviceRemoved:
                return "This device was removed"
            case let .throttled(retryAfter):
                return "The server asked for \(Int(retryAfter))s before the next try"
            case let .clockSkew(seconds):
                return "This device's clock is \(Int(seconds))s off the server's"
            case .offline:
                return "No connection"
            case let .server(failure):
                return failure.description
            }
        }
    }

    /// A reader context and the two facts about it that decide when it is
    /// replaced.
    ///
    /// `generation` counts mints, and it is what "give me a token newer than
    /// the one that just failed" is expressed in.  A timestamp would not do:
    /// two mints inside the same clock tick are indistinguishable by time, and
    /// a caller retrying a rejected token would either accept the dead one back
    /// or spin.  A counter cannot be ambiguous.
    private struct ReaderToken: Sendable {
        let value: String
        let generation: Int
        let expiresAt: Date
    }

    private let client: CloudClient
    private let credentials: DeviceCredentialStore
    private let cache: SnapshotCache
    private let clock: @Sendable () -> Date

    private var device: PairedDevice?
    private var token: ReaderToken?
    private var state: DeviceState
    private var lifecycleGeneration = 0

    private var mintCount = 0
    /// The one refresh allowed to be in flight. Every caller that arrives
    /// while it is set awaits it instead of signing a second one.
    private var refreshTask: Task<ReaderToken, Error>?
    private var refreshTaskID: Int?
    private var nextRefreshTaskID = 0
    /// One gate for the whole session. A `Retry-After` on a read is the same
    /// deployment saying the same thing as a `Retry-After` on a refresh, and
    /// honouring it on one route while hammering another is not backing off.
    private var nextAttemptAllowedAt: Date?
    private var consecutiveFailures = 0
    private var consecutiveRejections = 0

    init(
        client: CloudClient,
        credentials: DeviceCredentialStore,
        cache: SnapshotCache,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.client = client
        self.credentials = credentials
        self.cache = cache
        self.clock = clock
        let stored = credentials.load()
        self.device = stored
        self.state = stored == nil ? .unpaired : .paired
    }

    // MARK: - What the app asks

    var deviceState: DeviceState { state }

    var isPaired: Bool { device != nil }

    /// Last-known data, with no network attempt at all.
    ///
    /// This is what a cold start renders first: the screens have something real
    /// on them before the first request is built, and `load` then replaces it
    /// with the reconciled result. It reads one small file and cannot fail --
    /// an unreadable cache is simply nothing -- which is why it is `nonisolated`
    /// and callable from a view body without awaiting the actor.
    nonisolated func cached(_ route: CloudRoute) -> CloudSnapshot? {
        guard let cached = cache.load(route) else { return nil }
        return CloudSnapshot(
            route: route,
            revision: cached.revision,
            items: cached.items,
            source: .cache,
            asOf: cached.storedAt
        )
    }

    /// Redeem a pairing code.
    ///
    /// A successful pairing is a new identity, so it starts from nothing: the
    /// previous device's cached objects are not this one's, their revisions
    /// mean nothing against the new credential's scope, and a `removed` state
    /// left over from the credential being replaced would be wrong from the
    /// first request onward.
    @discardableResult
    func pair(code: String, label: String? = nil) async throws -> PairedDevice {
        let pairingGeneration = lifecycleGeneration
        let result: PairingResult
        do {
            result = try await client.pair(code: code, label: label)
        } catch {
            throw classify(error)
        }
        guard lifecycleGeneration == pairingGeneration else {
            throw lifecycleFailure()
        }
        // Cache first, credential second, and the order is not arbitrary: a
        // crash between them must not leave the new credential paired with the
        // old scope's checkpoint, which would make the first `since=` ask about
        // revisions from a scope this device has never read. Losing the cache
        // and not the credential is a cold start; the reverse is silent data
        // loss.
        cache.removeAll()
        try credentials.save(result.device)
        lifecycleGeneration += 1
        refreshTask = nil
        refreshTaskID = nil
        device = result.device
        state = .paired
        mintCount += 1
        token = ReaderToken(
            value: result.readerContext,
            generation: mintCount,
            expiresAt: clock().addingTimeInterval(
                Swift.min(result.expiresIn, Self.maximumContextLifetime)
            )
        )
        consecutiveFailures = 0
        consecutiveRejections = 0
        nextAttemptAllowedAt = nil
        return result.device
    }

    /// Forget this device locally: credential, token and cache.
    ///
    /// The same wipe revocation performs, minus the accusation. Ending a
    /// device's access on the server is the rider revoking it (#153); this is
    /// only the local half.
    func signOut() {
        lifecycleGeneration += 1
        credentials.clear()
        cache.removeAll()
        device = nil
        token = nil
        state = .unpaired
        refreshTask = nil
        refreshTaskID = nil
        consecutiveFailures = 0
        consecutiveRejections = 0
        nextAttemptAllowedAt = nil
    }

    func devices() async throws -> [CloudDevice] {
        guard state != .removed else { throw Failure.deviceRemoved }
        guard state == .paired, let device else { throw Failure.notPaired }
        do {
            return try await client.devices(for: device)
        } catch {
            throw classify(error)
        }
    }

    func removeDevice() async throws {
        guard state != .removed else { throw Failure.deviceRemoved }
        guard state == .paired, let activeDevice = device else { throw Failure.notPaired }
        let generation = lifecycleGeneration
        do {
            try await client.revoke(credentialID: activeDevice.credentialID, for: activeDevice)
        } catch {
            throw classify(error)
        }
        guard state == .paired, device == activeDevice, lifecycleGeneration == generation else {
            throw lifecycleFailure()
        }
        signOut()
    }

    /// A route's objects, reconciled with the server where that is possible.
    ///
    /// Never throws when there is a cache to serve: airplane mode, a 429, a
    /// token that cannot be refreshed and a server that is simply down all
    /// produce last-known data marked `.cache` rather than an error, which is
    /// what "an hour in airplane mode recovers cleanly" means in practice. The
    /// one exception is a removed device, where continuing to show the rider's
    /// data is the outcome being prevented.
    func load(_ route: CloudRoute) async throws -> CloudSnapshot {
        let readDevice = try activeDevice()
        let readLifecycleGeneration = lifecycleGeneration
        let cached = cache.load(route)
        do {
            let snapshot = try await reconcile(
                route, cached: cached, device: readDevice,
                lifecycleGeneration: readLifecycleGeneration
            )
            try validate(readDevice, lifecycleGeneration: readLifecycleGeneration)
            return snapshot
        } catch Failure.deviceRemoved {
            throw Failure.deviceRemoved
        } catch Failure.notPaired {
            // Cached objects with no credential behind them is a state the
            // rider has to be told about rather than shown data in: there is
            // nothing that can refresh it and nothing that could be told the
            // old credential was revoked.
            throw Failure.notPaired
        } catch {
            guard let cached else {
                try validate(readDevice, lifecycleGeneration: readLifecycleGeneration)
                throw error
            }
            try validate(readDevice, lifecycleGeneration: readLifecycleGeneration)
            return CloudSnapshot(
                route: route,
                revision: cached.revision,
                items: cached.items,
                source: .cache,
                asOf: cached.storedAt
            )
        }
    }

    private func activeDevice() throws -> PairedDevice {
        guard state != .removed else { throw Failure.deviceRemoved }
        guard state == .paired, let device else { throw Failure.notPaired }
        return device
    }

    private func validate(_ readDevice: PairedDevice, lifecycleGeneration: Int) throws {
        guard state != .removed else { throw Failure.deviceRemoved }
        guard state == .paired, device == readDevice,
              self.lifecycleGeneration == lifecycleGeneration else { throw Failure.notPaired }
    }

    // MARK: - The token

    /// A usable reader context, refreshing if one is needed.
    func readerContext() async throws -> String {
        try await context(after: nil).value
    }

    /// A reader context newer than generation `after`, refreshing if needed.
    ///
    /// `after` is how a caller says "the token I just used was rejected". It
    /// forces a token minted later than that one, so a refresh already in
    /// flight -- which may well be the one that produced the dead token --
    /// cannot be mistaken for the fix.
    private func context(after generation: Int?) async throws -> ReaderToken {
        // Bounded because every branch below returns, throws, or makes
        // progress; the bound is here so a mistake in that reasoning is a
        // failed request rather than a spin.
        for _ in 0..<4 {
            // Re-checked every pass, not once on the way in. A caller that
            // joined somebody else's refresh may be resuming into a session
            // that refresh has just removed, and it has to report *that*
            // rather than the backoff the failure also set.
            guard state != .removed else { throw Failure.deviceRemoved }
            guard device != nil else { throw Failure.notPaired }

            if let token, isUsable(token, at: clock()),
               generation.map({ token.generation > $0 }) ?? true {
                return token
            }
            if let inFlight = refreshTask {
                // Somebody is already refreshing. Join it rather than sign a
                // second refresh -- and join it whatever generation it began
                // at, because any token it mints is by definition newer than
                // one this caller already holds. The failure is swallowed on
                // purpose: it is the starter's to report, and this caller
                // re-evaluates instead, where the backoff gate below refuses
                // it if the failure was real.
                _ = try? await inFlight.value
                continue
            }
            let now = clock()
            if let allowed = nextAttemptAllowedAt, allowed > now {
                throw Failure.throttled(retryAfter: allowed.timeIntervalSince(now))
            }
            guard let refreshDevice = device else { throw Failure.notPaired }
            let refreshGeneration = lifecycleGeneration
            nextRefreshTaskID += 1
            let refreshID = nextRefreshTaskID
            let task = Task { () throws -> ReaderToken in
                defer {
                    if self.refreshTaskID == refreshID {
                        self.refreshTask = nil
                        self.refreshTaskID = nil
                    }
                }
                let fresh = try await self.performRefresh(
                    for: refreshDevice, lifecycleGeneration: refreshGeneration
                )
                guard self.isCurrent(
                    device: refreshDevice, lifecycleGeneration: refreshGeneration
                ), self.refreshTaskID == refreshID else {
                    throw self.lifecycleFailure()
                }
                self.token = fresh
                return fresh
            }
            refreshTask = task
            refreshTaskID = refreshID
            let fresh = try await task.value
            guard isCurrent(
                device: refreshDevice, lifecycleGeneration: refreshGeneration
            ) else { throw lifecycleFailure() }
            return fresh
        }
        throw Failure.throttled(retryAfter: Self.baseBackoff)
    }

    private func isUsable(_ token: ReaderToken, at now: Date) -> Bool {
        let remaining = token.expiresAt.timeIntervalSince(now)
        return remaining > Swift.max(Self.refreshAhead, Self.minimumUsableLifetime)
    }

    /// One signed refresh, and everything that follows from how it went.
    private func performRefresh(
        for device: PairedDevice, lifecycleGeneration: Int
    ) async throws -> ReaderToken {
        guard isCurrent(device: device, lifecycleGeneration: lifecycleGeneration) else {
            throw lifecycleFailure()
        }
        do {
            let outcome = try await client.refreshReaderContext(for: device)
            guard isCurrent(device: device, lifecycleGeneration: lifecycleGeneration) else {
                throw lifecycleFailure()
            }
            consecutiveFailures = 0
            consecutiveRejections = 0
            nextAttemptAllowedAt = nil
            mintCount += 1
            return ReaderToken(
                value: outcome.readerContext,
                generation: mintCount,
                expiresAt: clock().addingTimeInterval(
                    Swift.min(outcome.expiresIn, Self.maximumContextLifetime)
                )
            )
        } catch let failure as Failure {
            throw failure
        } catch let failure as CloudClient.Failure {
            guard isCurrent(device: device, lifecycleGeneration: lifecycleGeneration) else {
                throw lifecycleFailure()
            }
            throw refusal(failure)
        } catch {
            guard isCurrent(device: device, lifecycleGeneration: lifecycleGeneration) else {
                throw lifecycleFailure()
            }
            // A transport error is the network, not the credential: it must
            // never move this device toward `removed`, or a week in a valley
            // would unpair the rider's phone.
            noteFailure(retryAfter: nil)
            throw Failure.offline
        }
    }

    private func isCurrent(device: PairedDevice, lifecycleGeneration: Int) -> Bool {
        state == .paired && self.device == device
            && self.lifecycleGeneration == lifecycleGeneration
    }

    private func lifecycleFailure() -> Failure {
        state == .removed ? .deviceRemoved : .notPaired
    }

    /// What a refused refresh means, which is the sharpest question this
    /// client has to answer.
    ///
    /// The read plane answers **404 to every authentication failure** --
    /// unknown credential, revoked credential, bad signature, stale timestamp,
    /// replayed nonce, wrong attested subject, missing capability, and the
    /// public-API kill switch -- deliberately, so nothing about credential
    /// state is observable from a response. That is right for the server, and
    /// it means this client can never be *told* it was revoked. It can only
    /// observe that a correctly signed request stopped being accepted.
    ///
    /// So `removed` is inferred, and inferred conservatively, because acting on
    /// it destroys local state:
    ///
    /// - **Only 404 counts.** A 403 on this route is a quota refusal, reachable
    ///   only *after* the signature verified, and a 401 can only come from a
    ///   gateway in front of the app. Neither says anything about the
    ///   credential, and treating either as revocation would wipe a working
    ///   device over a billing threshold.
    /// - **Twice, with a backoff between.** A single 404 is also what a
    ///   deployment mid-restart or a flipped kill switch produces, and the gap
    ///   `noteFailure` enforces before the second attempt is sized to outlast
    ///   one -- at least half of `baseBackoff` (15s), scaling with repeated
    ///   failures. A rider pulling to refresh twice inside that window gets
    ///   throttled, not unpaired.
    /// - **Never while the clock is suspect -- including when that cannot be
    ///   checked.** A device more than four minutes off the server's clock has
    ///   its signed timestamp refused on every attempt, forever, and would
    ///   otherwise conclude it had been revoked. The server's `Date` header
    ///   arrives on the 404 itself, so ruling this out costs no extra request
    ///   -- but a `Date` that is missing, or in one of the two obsolete
    ///   formats `HTTPHeaderDates` does not parse, means skew cannot be ruled
    ///   out either. That rejection is never counted: the alternative treats
    ///   "cannot tell" as "clock is fine", which is the opposite of what a
    ///   fail-safe reading of an unreadable clock means.
    ///
    /// The residual false positive is the kill switch: a deployment that turns
    /// the public API off for longer than the backoff window unpairs these
    /// phones and the rider pairs again when it returns. That is the
    /// direction to be wrong in. The opposite -- a revoked phone still
    /// showing the rider's training data because the client would rather not
    /// be hasty -- is the outcome revocation exists to prevent.
    private func refusal(_ failure: CloudClient.Failure) -> Failure {
        guard case let .http(status, _, retryAfter, serverDate) = failure else {
            noteFailure(retryAfter: nil)
            return .server(failure)
        }
        if status == 404 {
            guard let serverDate else {
                // No clock reference on this response -- the header was
                // absent, or it was in one of the two obsolete formats
                // `HTTPHeaderDates` refuses to parse -- so a skewed device
                // clock cannot be ruled out. Counting this rejection anyway
                // would misdiagnose that skew as revocation the first time a
                // 404 happens to arrive with no readable `Date`; failing safe
                // means this sample is simply thrown away rather than banked.
                noteFailure(retryAfter: nil)
                return .server(failure)
            }
            let skew = serverDate.timeIntervalSince(clock())
            if abs(skew) > Self.clockSkewTolerance {
                noteFailure(retryAfter: nil)
                return .clockSkew(seconds: skew)
            }
            consecutiveRejections += 1
            if consecutiveRejections >= Self.rejectionsBeforeRemoval {
                markRemoved()
                return .deviceRemoved
            }
            noteFailure(retryAfter: nil)
            return .server(failure)
        }
        noteFailure(retryAfter: retryAfter)
        if status == 429 || status == 503 || status == 403 {
            return .throttled(retryAfter: retryAfter ?? pendingDelay())
        }
        return .server(failure)
    }

    /// The device is gone. Leave nothing behind that could still be read.
    private func markRemoved() {
        lifecycleGeneration += 1
        state = .removed
        token = nil
        device = nil
        refreshTask = nil
        refreshTaskID = nil
        credentials.clear()
        cache.removeAll()
    }

    /// Push the next attempt out.
    ///
    /// A server-supplied `Retry-After` is taken exactly -- it is the deployment
    /// saying when it will be ready, and jittering an answer we asked for would
    /// be second-guessing it. Only where the server said nothing does this
    /// client invent a delay, and then it is exponential with full jitter: two
    /// phones that lost their connection at the same moment must not come back
    /// in step, and the app has a cache to show while it waits.
    private func noteFailure(retryAfter: TimeInterval?) {
        consecutiveFailures += 1
        let delay: TimeInterval
        if let retryAfter {
            delay = retryAfter
        } else {
            let exponent = Swift.min(consecutiveFailures, 8)
            let ceiling = Swift.min(
                Self.maximumBackoff, Self.baseBackoff * pow(2, Double(exponent - 1))
            )
            // Equal jitter, not full jitter: the floor is half the ceiling,
            // not one second. Full jitter's near-zero tail let a second
            // pull-to-refresh land on the elapsed side of the gate almost
            // immediately, which made two-strike removal a coin flip rather
            // than a guarantee that a plausible outage had time to pass.
            // Halving the range still spreads retries wide enough to avoid a
            // thundering herd; it just never collapses to nothing.
            delay = Double.random(in: (ceiling / 2)...ceiling)
        }
        nextAttemptAllowedAt = clock().addingTimeInterval(delay)
    }

    private func pendingDelay() -> TimeInterval {
        guard let allowed = nextAttemptAllowedAt else { return Self.baseBackoff }
        return Swift.max(allowed.timeIntervalSince(clock()), 0)
    }

    private func classify(_ error: Error) -> Failure {
        if let failure = error as? Failure { return failure }
        if let failure = error as? CloudClient.Failure { return .server(failure) }
        if error is URLError { return .offline }
        return .server(.malformedResponse("\(type(of: error))"))
    }

    // MARK: - Reading a collection

    private func reconcile(
        _ route: CloudRoute, cached: CachedCollection?, device: PairedDevice,
        lifecycleGeneration: Int
    ) async throws -> CloudSnapshot {
        if let allowed = nextAttemptAllowedAt, allowed > clock() {
            throw Failure.throttled(retryAfter: allowed.timeIntervalSince(clock()))
        }
        // A delta is asked for only where the route serves one AND there is
        // something to be a delta from. `since=0` and no `since` at all are
        // both full reads, but only the former also carries tombstones, and
        // asking for tombstones against an empty cache is pure cost.
        let since = route.servesDeltas ? cached?.revision : nil
        var cursor: String?
        var received: [CloudItem] = []
        var revision = since ?? 0
        var pages = 0

        repeat {
            let response = try await page(
                route, device: device, since: since, cursor: cursor
            )
            try validate(device, lifecycleGeneration: lifecycleGeneration)
            received.append(contentsOf: response.items)
            // Every page of one walk reports the checkpoint pinned when paging
            // began -- the server carries it inside the signed cursor -- so
            // this is the same value on each pass, not a moving target.
            if let served = response.revision { revision = Swift.max(revision, served) }
            cursor = response.nextCursor
            pages += 1
        } while cursor != nil && pages < Self.maximumPages

        let merged = merge(received, into: since == nil ? nil : cached)
        // A truncated walk must not advance the checkpoint: the objects on the
        // pages never fetched would be skipped forever, because `since=` never
        // resends them. Keeping the old revision replays this delta next time,
        // which is the at-least-once side of the contract to be on.
        let complete = cursor == nil
        let now = clock()
        if complete {
            try validate(device, lifecycleGeneration: lifecycleGeneration)
            cache.store(
                CachedCollection(revision: revision, items: merged, storedAt: now),
                for: route
            )
        }
        try validate(device, lifecycleGeneration: lifecycleGeneration)
        return CloudSnapshot(
            route: route,
            revision: complete ? revision : (cached?.revision ?? revision),
            items: merged,
            source: .network,
            asOf: now
        )
    }

    /// One page, with exactly one forced refresh if the reader context was the
    /// problem.
    ///
    /// A 404 here means the bearer context was rejected -- expired between two
    /// requests, most likely. It is retried once against a token minted after
    /// the one that failed. A second 404, with a context the server itself has
    /// just issued, is not an authentication problem and is reported as what it
    /// is rather than laundered into another refresh.
    ///
    /// Note what this path deliberately does *not* do: it never counts toward
    /// revocation. Only the signed refresh, which proves possession of the
    /// device key, is allowed to decide that this device is gone.
    private func page(
        _ route: CloudRoute, device: PairedDevice, since: Int?, cursor: String?
    ) async throws -> CollectionResponse {
        let attempt: ReaderToken
        do {
            attempt = try await context(after: nil)
        } catch {
            throw classify(error)
        }
        do {
            return try await client.collection(
                route, readerContext: attempt.value, device: device,
                since: since, cursor: cursor
            )
        } catch let failure as CloudClient.Failure {
            guard case let .http(status, _, retryAfter, _) = failure else {
                throw Failure.server(failure)
            }
            if status == 429 || status == 503 {
                noteFailure(retryAfter: retryAfter)
                throw Failure.throttled(retryAfter: retryAfter ?? pendingDelay())
            }
            guard status == 404 else { throw Failure.server(failure) }
            let renewed = try await context(after: attempt.generation)
            do {
                return try await client.collection(
                    route, readerContext: renewed.value, device: device,
                    since: since, cursor: cursor
                )
            } catch let retried as CloudClient.Failure {
                throw Failure.server(retried)
            }
        } catch {
            throw classify(error)
        }
    }

    /// Apply what arrived to what was cached.
    ///
    /// `cached == nil` is a full read and replaces outright. Otherwise this is
    /// a delta: an object carrying `deleted` is a tombstone and removes its id,
    /// everything else is an upsert. The result is ordered by object id, which
    /// is the order the server pages in, so a merged collection reads the same
    /// way a freshly fetched one does.
    private func merge(
        _ received: [CloudItem], into cached: CachedCollection?
    ) -> [CloudItem] {
        var byID: [String: CloudItem] = [:]
        for item in cached?.items ?? [] { byID[item.id] = item }
        for item in received {
            if item.deleted {
                byID.removeValue(forKey: item.id)
            } else {
                byID[item.id] = item
            }
        }
        return byID.values.sorted { $0.id < $1.id }
    }
}
