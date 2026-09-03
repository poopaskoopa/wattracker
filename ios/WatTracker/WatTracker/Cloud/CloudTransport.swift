import Foundation

/// One HTTP exchange, reduced to the four things this client reasons about.
///
/// `retryAfter` and `serverDate` are parsed here rather than at the call site
/// because both are header formats with more than one legal spelling, and a
/// client that gets either wrong misbehaves quietly: an unparsed `Retry-After`
/// becomes a hammering loop, and an unparsed `Date` becomes a device that
/// blames the server for its own clock.
struct CloudResponse: Sendable {
    let status: Int
    let body: Data
    /// `Retry-After`, in seconds from now, however the header spelled it.
    let retryAfter: TimeInterval?
    /// The server's `Date`.  The one clock reference available for free on
    /// every response, including the ones that refuse the request.
    let serverDate: Date?
}

/// The single place a request leaves the app.
///
/// A protocol so the whole token lifecycle -- expiry, coalescing, backoff,
/// revocation -- can be tested against scripted responses at the exact instant
/// it matters, with no network, no server and no sleeping.  It is deliberately
/// not a `URLSession` subclass or a `URLProtocol` stub: both of those test the
/// client through the machinery they are supposed to be standing in for.
protocol CloudTransport: Sendable {
    func send(_ request: URLRequest) async throws -> CloudResponse
}

/// The shipping transport.
///
/// Certificate validation is on and there is no way to turn it off, which is a
/// property of what is *absent* here: this session has no delegate, so
/// `urlSession(_:didReceive:completionHandler:)` -- the only hook that can
/// accept a server trust the system rejected -- does not exist to be called.
/// There is no debug branch, no `#if DEBUG` bypass and no configuration flag,
/// because a bypass that exists in a debug build is one build setting away
/// from shipping.  The local-http affordance a developer actually needs lives
/// in `Info.plist` as an ATS exception scoped to `localhost` and the local
/// network, where it cannot reach `api.wattracker.com`.
struct URLSessionCloudTransport: CloudTransport {
    /// One session for the app. A `URLSession` owns a connection pool and a
    /// delegate queue; building one per client would throw away connection
    /// reuse on exactly the requests that benefit from it most.
    static let shared = URLSessionCloudTransport()

    private let session: URLSession

    init(session: URLSession? = nil) {
        self.session = session ?? Self.makeSession()
    }

    static func makeSession() -> URLSession {
        // Ephemeral: every read-plane response is `Cache-Control: no-store`
        // and carries the rider's training data, so the URL cache would be a
        // second, unprotected copy of everything `SnapshotCache` is careful
        // about. Cookies and credential storage go with it -- this API
        // authenticates with a signature and a bearer context and has no use
        // for either.
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpShouldSetCookies = false
        configuration.tlsMinimumSupportedProtocolVersion = .TLSv12
        // Airplane mode must fail now, not later. The app's answer to no
        // network is the cache, and it cannot show it while a request sits
        // waiting for connectivity that may not return for an hour.
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 60
        return URLSession(configuration: configuration)
    }

    func send(_ request: URLRequest) async throws -> CloudResponse {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        return CloudResponse(
            status: http.statusCode,
            body: data,
            retryAfter: HTTPHeaderDates.retryAfterSeconds(
                http.value(forHTTPHeaderField: "Retry-After")
            ),
            serverDate: HTTPHeaderDates.date(
                http.value(forHTTPHeaderField: "Date")
            )
        )
    }
}

/// The two header formats this client reads.
enum HTTPHeaderDates {
    /// RFC 9110's preferred `IMF-fixdate`, which is what every one of these
    /// servers emits.  The two obsolete formats are not parsed: a header this
    /// fails to read is treated as absent.  An absent `Date` is not "safe to
    /// ignore" on its own -- it is `CloudSession.refusal(_:)` that makes it
    /// safe, by treating a 404 with no clock reference as unable to rule out a
    /// skewed device clock and never letting that rejection count toward
    /// removal.  Skipping the clock check here while the caller still banked
    /// the refusal underneath it would be exactly backwards.
    ///
    /// Shared, and never mutated after construction: `DateFormatter` is
    /// documented thread-safe for formatting and parsing on iOS 7 and later,
    /// and building one per response is famously the expensive way to do this.
    static let imfFixdate: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        return formatter
    }()

    static func date(_ header: String?) -> Date? {
        guard let header, !header.isEmpty else { return nil }
        return imfFixdate.date(from: header.trimmingCharacters(in: .whitespaces))
    }

    /// `Retry-After` as seconds from now.
    ///
    /// Clamped at both ends. Zero or negative means "now", which is a legal
    /// answer that must not become a busy loop, so it floors at one second. A
    /// wildly large value -- a misconfigured gateway, or a date parsed against
    /// a badly skewed device clock -- would otherwise wedge the app for a week,
    /// so it caps at an hour; past that the difference between waiting and
    /// having stopped is not one the rider can perceive anyway.
    static func retryAfterSeconds(
        _ header: String?, now: Date = Date()
    ) -> TimeInterval? {
        guard let header else { return nil }
        let trimmed = header.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        let seconds: TimeInterval
        if let delta = TimeInterval(trimmed) {
            seconds = delta
        } else if let date = date(trimmed) {
            seconds = date.timeIntervalSince(now)
        } else {
            return nil
        }
        return Swift.min(Swift.max(seconds, 1), 3600)
    }
}
