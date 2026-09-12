import Foundation

struct LocalResponse: Sendable {
    let status: Int
    let body: Data
    let url: URL
    let retryAfter: TimeInterval?
    let serverDate: Date?

    init(status: Int, body: Data, url: URL,
         retryAfter: TimeInterval? = nil, serverDate: Date? = nil) {
        self.status = status
        self.body = body
        self.url = url
        self.retryAfter = retryAfter
        self.serverDate = serverDate
    }
}

protocol LocalTransport: Sendable {
    func send(_ request: URLRequest) async throws -> LocalResponse
}

/// Redirects are limited to the configured desktop origin. There is no trust
/// delegate here: system TLS validation remains the only certificate policy.
private final class LocalRedirectDelegate: NSObject, URLSessionTaskDelegate {
    let origin: URL

    init(origin: URL) {
        self.origin = origin
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let target = request.url, sameOrigin(origin, target) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }

    private func sameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
        lhs.scheme?.lowercased() == rhs.scheme?.lowercased()
            && lhs.host?.lowercased() == rhs.host?.lowercased()
            && (lhs.port ?? 443) == (rhs.port ?? 443)
    }
}

struct URLSessionLocalTransport: LocalTransport {
    private let session: URLSession

    init(baseURL: URL, session: URLSession? = nil) {
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.urlCache = nil
            configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
            configuration.httpCookieAcceptPolicy = .always
            configuration.httpShouldSetCookies = true
            configuration.tlsMinimumSupportedProtocolVersion = .TLSv12
            configuration.waitsForConnectivity = false
            configuration.timeoutIntervalForRequest = 20
            configuration.timeoutIntervalForResource = 60
            self.session = URLSession(
                configuration: configuration,
                delegate: LocalRedirectDelegate(origin: baseURL),
                delegateQueue: nil
            )
        }
    }

    func send(_ request: URLRequest) async throws -> LocalResponse {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, let url = http.url else {
            throw URLError(.badServerResponse)
        }
        return LocalResponse(
            status: http.statusCode,
            body: data,
            url: url,
            retryAfter: HTTPHeaderDates.retryAfterSeconds(
                http.value(forHTTPHeaderField: "Retry-After")
            ),
            serverDate: HTTPHeaderDates.date(http.value(forHTTPHeaderField: "Date"))
        )
    }
}

struct LocalClient: Sendable {
    enum Failure: Error, CustomStringConvertible {
        case insecureOrInvalidBaseURL
        case missingToken
        case unauthorized
        case http(status: Int, path: String, retryAfter: TimeInterval?)
        case malformedResponse(String)

        var description: String {
            switch self {
            case .insecureOrInvalidBaseURL: return "The desktop address must use HTTPS"
            case .missingToken: return "The desktop token is missing"
            case .unauthorized: return "The local device token was refused"
            case let .http(status, path, _): return "HTTP \(status) from \(path)"
            case let .malformedResponse(path): return "Malformed response from \(path)"
            }
        }
    }

    private struct Ticket: Decodable {
        let ticket: String
    }

    private struct VolumeResponse: Decodable {
        let weeks: [VolumeWeek]
    }

    private struct CalendarResponse: Decodable {
        let weeks: [[CalendarDay]]
    }

    let baseURL: URL
    let token: String
    let transport: any LocalTransport

    init(baseURL: URL, token: String, transport: (any LocalTransport)? = nil) throws {
        guard baseURL.scheme?.lowercased() == "https",
              baseURL.host != nil,
              baseURL.user == nil,
              baseURL.password == nil,
              baseURL.query == nil,
              baseURL.fragment == nil,
              baseURL.path.isEmpty || baseURL.path == "/"
        else { throw Failure.insecureOrInvalidBaseURL }
        let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { throw Failure.missingToken }
        self.baseURL = baseURL
        self.token = value
        self.transport = transport ?? URLSessionLocalTransport(baseURL: baseURL)
    }

    /// Opens the cookie session using the connector token. The ticket is
    /// single-use and is never persisted.
    func authenticate() async throws {
        var request = URLRequest(url: try endpoint("/api/connector/session"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let minted: Ticket = try await sendJSON(request, expectedPath: "/api/connector/session")
        guard !minted.ticket.isEmpty else {
            throw Failure.malformedResponse("/api/connector/session")
        }

        let redeemURL = try endpoint("/connector/session", query: [
            URLQueryItem(name: "token", value: minted.ticket),
        ])
        var redeem = URLRequest(url: redeemURL)
        redeem.httpMethod = "GET"
        let response = try await transport.send(redeem)
        guard (200..<300).contains(response.status) else {
            throw map(response, path: "/connector/session")
        }
        // A valid ticket lands on the authenticated home page. Reject any
        // other landing path, including /login or /welcome, even if it is a 200.
        guard response.url.path == "/" else { throw Failure.unauthorized }
    }

    func trainingState() async throws -> TrainingState {
        try await get("/api/state")
    }

    func loadPoints(months: Double? = nil) async throws -> [LoadPoint] {
        var query: [URLQueryItem] = []
        if let months { query.append(URLQueryItem(name: "months", value: String(months))) }
        return try await get("/api/load", query: query)
    }

    func curve() async throws -> PowerCurve {
        try await get("/api/curve")
    }

    func activities() async throws -> [ActivitySummary] {
        try await get("/api/activities")
    }

    func volume() async throws -> [VolumeWeek] {
        let response: VolumeResponse = try await get("/api/volume")
        return response.weeks
    }

    func calendar(year: Int, month: Int) async throws -> [CalendarDay] {
        let response: CalendarResponse = try await get(
            "/api/calendar",
            query: [
                URLQueryItem(name: "year", value: String(year)),
                URLQueryItem(name: "month", value: String(month)),
            ]
        )
        return response.weeks.flatMap { $0 }
    }

    func activityDetail(_ activityID: Int) async throws -> ActivityDetail {
        try await get("/api/activity/\(activityID)")
    }

    func activityStreams(_ activityID: Int) async throws -> ActivityStreams {
        struct StreamResponse: Decodable {
            let t: [Double?]?
            let power: [Double?]?
            let heartrate: [Double?]?
            let cadence: [Double?]?
            let altitude: [Double?]?
        }
        let value: StreamResponse = try await get("/api/activity/\(activityID)")
        return ActivityStreams(
            streams: ActivityStreams.Channels(
                time: value.t,
                power: value.power,
                heartrate: value.heartrate,
                cadence: value.cadence,
                altitude: value.altitude
            )
        )
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        var request = URLRequest(url: try endpoint(path, query: query))
        request.httpMethod = "GET"
        return try await sendJSON(request, expectedPath: path)
    }

    private func sendJSON<T: Decodable>(
        _ request: URLRequest, expectedPath: String
    ) async throws -> T {
        let response = try await transport.send(request)
        guard (200..<300).contains(response.status) else {
            throw map(response, path: expectedPath)
        }
        // URLSession follows the desktop auth middleware's redirect to /login;
        // accepting that HTML as a JSON response would hide a revoked token.
        guard response.url.path == expectedPath else { throw Failure.unauthorized }
        do {
            return try JSONDecoder().decode(T.self, from: response.body)
        } catch {
            throw Failure.malformedResponse(expectedPath)
        }
    }

    private func map(_ response: LocalResponse, path: String) -> Failure {
        if response.status == 401 || response.status == 403 {
            return .unauthorized
        }
        return .http(status: response.status, path: path, retryAfter: response.retryAfter)
    }

    private func endpoint(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        components?.path = path
        components?.queryItems = query.isEmpty ? nil : query
        guard let url = components?.url, url.scheme?.lowercased() == "https" else {
            throw Failure.insecureOrInvalidBaseURL
        }
        return url
    }
}

/// The local session adapts the desktop JSON endpoints to the same snapshots
/// consumed by the cloud-backed screen models.
actor LocalSession: ReadSession {
    private let credentials: LocalCredentialStore
    private let cache: SnapshotCache
    private let makeClient: @Sendable (LocalCredential) throws -> LocalClient
    private let clock: @Sendable () -> Date

    private var credential: LocalCredential?
    private var client: LocalClient?
    private var state: CloudSession.DeviceState
    private var lastSuccessfulRead: Date?
    private var lifecycleGeneration = 0
    private var authenticated = false
    private var details: [Int: ActivityDetail] = [:]
    private var streams: [Int: ActivityStreams] = [:]

    init(
        credentials: LocalCredentialStore,
        cache: SnapshotCache,
        makeClient: @escaping @Sendable (LocalCredential) throws -> LocalClient = LocalSession.liveClient,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.credentials = credentials
        self.cache = cache
        self.makeClient = makeClient
        self.clock = clock
        let stored = credentials.load()
        self.credential = stored
        self.client = stored.flatMap { try? makeClient($0) }
        self.state = stored == nil ? .unpaired : .paired
        self.lastSuccessfulRead = nil
    }

    nonisolated static let liveClient: @Sendable (LocalCredential) throws -> LocalClient = { credential in
        guard let url = URL(string: credential.baseURL) else {
            throw LocalClient.Failure.insecureOrInvalidBaseURL
        }
        return try LocalClient(baseURL: url, token: credential.token)
    }

    var deviceState: CloudSession.DeviceState { state }

    var lastSuccess: Date? { lastSuccessfulRead }

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

    var pairingAddress: String? { credential?.baseURL }

    func pair(host: String, token: String, label: String? = nil) async throws {
        let pairingGeneration = lifecycleGeneration
        let candidate = try LocalCredential(baseURL: host, token: token, label: label)
        let candidateClient = try makeClient(candidate)
        do {
            try await candidateClient.authenticate()
            _ = try await candidateClient.trainingState()
        } catch {
            throw Self.publicFailure(error)
        }
        guard lifecycleGeneration == pairingGeneration else {
            throw state == .removed
                ? CloudSession.Failure.deviceRemoved
                : CloudSession.Failure.notPaired
        }
        try credentials.save(candidate)
        lifecycleGeneration += 1
        cache.removeAll()
        details.removeAll()
        streams.removeAll()
        credential = candidate
        client = candidateClient
        authenticated = true
        state = .paired
        lastSuccessfulRead = clock()
    }

    func signOut() {
        lifecycleGeneration += 1
        credentials.clear()
        cache.removeAll()
        details.removeAll()
        streams.removeAll()
        credential = nil
        client = nil
        authenticated = false
        state = .unpaired
        lastSuccessfulRead = nil
    }

    func probe() async {
        guard state == .paired else { return }
        let generation = lifecycleGeneration
        do {
            _ = try await perform { try await $0.trainingState() }
            guard lifecycleGeneration == generation, state == .paired else { return }
            lastSuccessfulRead = clock()
        } catch LocalClient.Failure.unauthorized {
            markRemoved()
        } catch {
            // A temporary local outage must not erase a still-valid token.
        }
    }

    func load(_ route: CloudRoute) async throws -> CloudSnapshot {
        guard state != .removed else { throw CloudSession.Failure.deviceRemoved }
        guard state == .paired else { throw CloudSession.Failure.notPaired }
        let generation = lifecycleGeneration
        let cached = cache.load(route)
        do {
            let items = try await read(route)
            try validate(generation)
            let now = clock()
            let snapshot = CloudSnapshot(
                route: route,
                revision: 0,
                items: items,
                source: .network,
                asOf: now
            )
            cache.store(CachedCollection(revision: 0, items: items, storedAt: now), for: route)
            lastSuccessfulRead = now
            return snapshot
        } catch let failure as CloudSession.Failure {
            throw failure
        } catch LocalClient.Failure.unauthorized {
            markRemoved()
            throw CloudSession.Failure.deviceRemoved
        } catch {
            guard let cached else { throw Self.publicFailure(error) }
            try validate(generation)
            return CloudSnapshot(
                route: route,
                revision: cached.revision,
                items: cached.items,
                source: .cache,
                asOf: cached.storedAt
            )
        }
    }

    func activityDetail(_ activityID: Int) async throws -> ActivityDetail {
        guard state != .removed else { throw CloudSession.Failure.deviceRemoved }
        guard state == .paired else { throw CloudSession.Failure.notPaired }
        let generation = lifecycleGeneration
        if let detail = details[activityID] { return detail }
        do {
            let detail = try await perform { try await $0.activityDetail(activityID) }
            try validate(generation)
            details[activityID] = detail
            lastSuccessfulRead = clock()
            return detail
        } catch LocalClient.Failure.unauthorized {
            markRemoved()
            throw CloudSession.Failure.deviceRemoved
        } catch {
            throw Self.publicFailure(error)
        }
    }

    func activityStreams(_ activityID: Int) async throws -> ActivityStreams {
        guard state != .removed else { throw CloudSession.Failure.deviceRemoved }
        guard state == .paired else { throw CloudSession.Failure.notPaired }
        let generation = lifecycleGeneration
        if let value = streams[activityID] { return value }
        do {
            let value = try await perform { try await $0.activityStreams(activityID) }
            try validate(generation)
            streams[activityID] = value
            lastSuccessfulRead = clock()
            return value
        } catch LocalClient.Failure.unauthorized {
            markRemoved()
            throw CloudSession.Failure.deviceRemoved
        } catch {
            throw Self.publicFailure(error)
        }
    }

    private func read(_ route: CloudRoute) async throws -> [CloudItem] {
        try await perform { client in
            switch route {
            case .dashboard:
                let training = try await client.trainingState()
                let points = try await client.loadPoints(months: 12)
                let curve = try await client.curve()
                var items = [CloudItem(
                    id: "training-state", kind: .trainingState, revision: 0,
                    payload: .trainingState(training)
                )]
                if let ftp = training.ftp {
                    items.append(CloudItem(
                        id: "profile", kind: .profile, revision: 0,
                        payload: .profile(RiderProfile(
                            displayName: nil, ftp: ftp, ftpWatts: nil, power: nil,
                            heartRate: nil, weightKg: nil, weightDate: nil,
                            weightSource: nil
                        ))
                    ))
                }
                items.append(CloudItem(id: "curve", kind: .curve, revision: 0, payload: .curve(curve)))
                items.append(contentsOf: points.enumerated().map { index, point in
                    CloudItem(
                        id: "load-\(point.date ?? index.description)", kind: .loadPoint,
                        revision: 0, payload: .loadPoint(point)
                    )
                })
                return items
            case .volume:
                let weeks = try await client.volume()
                return weeks.enumerated().map { index, week in
                    CloudItem(
                        id: "volume-\(week.weekStart ?? index.description)", kind: .volumeWeek,
                        revision: 0, payload: .volumeWeek(week)
                    )
                }
            case .activities:
                let activities = try await client.activities()
                return activities.enumerated().map { index, activity in
                    let id = activity.id.map { String(Int($0)) } ?? index.description
                    return CloudItem(id: "activity-\(id)", kind: .activity, revision: 0,
                                     payload: .activity(activity))
                }
            case .calendar:
                let month = CalendarMonth.current(date: clock())
                let days = try await client.calendar(year: month.year, month: month.month)
                return days.enumerated().map { index, day in
                        CloudItem(id: "calendar-\(day.date ?? index.description)",
                                  kind: .calendarDay, revision: 0, payload: .calendarDay(day))
                    }
            case .curve:
                return [CloudItem(id: "curve", kind: .curve, revision: 0,
                                  payload: .curve(try await client.curve()))]
            case .profile:
                let training = try await client.trainingState()
                return [CloudItem(id: "profile", kind: .profile, revision: 0,
                                  payload: .profile(RiderProfile(
                                    displayName: nil, ftp: training.ftp, ftpWatts: nil,
                                    power: nil, heartRate: nil, weightKg: nil,
                                    weightDate: nil, weightSource: nil
                                  )))]
            case .races:
                return []
            }
        }
    }

    private func perform<T>(
        _ operation: (LocalClient) async throws -> T
    ) async throws -> T {
        let generation = lifecycleGeneration
        guard let client else { throw LocalClient.Failure.insecureOrInvalidBaseURL }
        try await ensureAuthenticated()
        try validate(generation)
        do {
            let value = try await operation(client)
            try validate(generation)
            return value
        } catch LocalClient.Failure.unauthorized {
            try validate(generation)
            authenticated = false
            try await ensureAuthenticated()
            try validate(generation)
            let value = try await operation(client)
            try validate(generation)
            return value
        }
    }

    private func ensureAuthenticated() async throws {
        guard !authenticated else { return }
        guard let client else { throw LocalClient.Failure.insecureOrInvalidBaseURL }
        try await client.authenticate()
        authenticated = true
    }

    private func markRemoved() {
        lifecycleGeneration += 1
        credentials.clear()
        cache.removeAll()
        details.removeAll()
        streams.removeAll()
        credential = nil
        client = nil
        authenticated = false
        state = .removed
        lastSuccessfulRead = nil
    }

    private func validate(_ generation: Int) throws {
        guard lifecycleGeneration == generation else {
            throw state == .removed
                ? CloudSession.Failure.deviceRemoved
                : CloudSession.Failure.notPaired
        }
        switch state {
        case .paired:
            return
        case .removed:
            throw CloudSession.Failure.deviceRemoved
        case .unpaired:
            throw CloudSession.Failure.notPaired
        }
    }

    private static func publicFailure(_ error: Error) -> CloudSession.Failure {
        if let failure = error as? CloudSession.Failure { return failure }
        if error is URLError { return .offline }
        if let failure = error as? LocalClient.Failure {
            switch failure {
            case .unauthorized: return .deviceRemoved
            case let .http(status, path, retryAfter):
                if status == 429 || status == 503 {
                    return .throttled(retryAfter: retryAfter ?? 30)
                }
                return .server(.malformedResponse("local HTTP \(status) from \(path)"))
            case let .malformedResponse(path):
                return .server(.malformedResponse("local \(path)"))
            case .insecureOrInvalidBaseURL, .missingToken:
                return .server(.malformedResponse(failure.description))
            }
        }
        return .server(.malformedResponse("local response"))
    }
}
