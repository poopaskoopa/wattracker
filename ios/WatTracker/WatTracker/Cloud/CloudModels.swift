import Foundation

/// The read plane's wire format, as types instead of dictionary lookups.
///
/// Every object the desktop publishes is `{id, kind, revision, data}` plus an
/// optional `deleted` marker, and `kind` is what decides how `data` reads.
/// That is the whole envelope; `wattracker/cloud/models.py:CloudObject.wire`
/// is the other half of it.
///
/// Two decoding rules are load-bearing rather than stylistic:
///
/// 1. **An unknown kind decodes, it does not throw.** The `since=` protocol
///    resends nothing: once the client acknowledges a checkpoint by asking for
///    the next one, an object it dropped is gone until the desktop happens to
///    republish it.  A single unmodelled kind that failed the whole page would
///    take the modelled objects on that page with it.
/// 2. **A tombstone carries no payload.** The server sends `data: {}` for a
///    deleted object, so decoding it as its kind would fail on every required
///    field.  `deleted` is read first and the payload is skipped.
enum CloudKind: Sendable, Equatable, Hashable {
    case profile
    case trainingState
    case ftpHistory
    case loadPoint
    case curve
    case volumeWeek
    case calendarDay
    case activity
    case activityDetail
    case stream
    /// A kind this build does not model.  Carried, never discarded.
    case other(String)

    init(wire: String) {
        switch wire {
        case "profile": self = .profile
        case "training_state": self = .trainingState
        case "ftp_history": self = .ftpHistory
        case "load_point": self = .loadPoint
        case "curve": self = .curve
        case "volume_week": self = .volumeWeek
        case "calendar_day": self = .calendarDay
        case "activity": self = .activity
        case "activity_detail": self = .activityDetail
        case "stream": self = .stream
        default: self = .other(wire)
        }
    }

    var wire: String {
        switch self {
        case .profile: return "profile"
        case .trainingState: return "training_state"
        case .ftpHistory: return "ftp_history"
        case .loadPoint: return "load_point"
        case .curve: return "curve"
        case .volumeWeek: return "volume_week"
        case .calendarDay: return "calendar_day"
        case .activity: return "activity"
        case .activityDetail: return "activity_detail"
        case .stream: return "stream"
        case let .other(value): return value
        }
    }
}

/// One published object.
struct CloudItem: Codable, Sendable, Equatable {
    let id: String
    let kind: CloudKind
    let revision: Int
    let deleted: Bool
    let payload: CloudPayload

    private enum CodingKeys: String, CodingKey {
        case id, kind, revision, data, deleted
    }

    init(id: String, kind: CloudKind, revision: Int, deleted: Bool = false,
         payload: CloudPayload) {
        self.id = id
        self.kind = kind
        self.revision = revision
        self.deleted = deleted
        self.payload = payload
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        kind = CloudKind(wire: try container.decode(String.self, forKey: .kind))
        revision = try container.decode(Int.self, forKey: .revision)
        deleted = try container.decodeIfPresent(Bool.self, forKey: .deleted) ?? false
        if deleted {
            // `data` is `{}` on a tombstone. Reading it as the kind's payload
            // would fail on the first non-optional field and take the whole
            // page down with it.
            payload = .tombstone
            return
        }
        payload = try CloudPayload(kind: kind, container: container, key: .data)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(kind.wire, forKey: .kind)
        try container.encode(revision, forKey: .revision)
        if deleted { try container.encode(true, forKey: .deleted) }
        try payload.encode(into: &container, key: .data)
    }
}

/// `data`, read as whatever `kind` says it is.
enum CloudPayload: Sendable, Equatable {
    case profile(RiderProfile)
    case trainingState(TrainingState)
    case ftpHistory(FTPHistoryPoint)
    case loadPoint(LoadPoint)
    case curve(PowerCurve)
    case volumeWeek(VolumeWeek)
    case calendarDay(CalendarDay)
    case activity(ActivitySummary)
    case activityDetail(ActivityDetail)
    case stream(ActivityStreams)
    case other(JSONValue)
    case tombstone

    fileprivate init<Key: CodingKey>(
        kind: CloudKind, container: KeyedDecodingContainer<Key>, key: Key
    ) throws {
        switch kind {
        case .profile:
            self = .profile(try container.decode(RiderProfile.self, forKey: key))
        case .trainingState:
            self = .trainingState(try container.decode(TrainingState.self, forKey: key))
        case .ftpHistory:
            self = .ftpHistory(try container.decode(FTPHistoryPoint.self, forKey: key))
        case .loadPoint:
            self = .loadPoint(try container.decode(LoadPoint.self, forKey: key))
        case .curve:
            self = .curve(try container.decode(PowerCurve.self, forKey: key))
        case .volumeWeek:
            self = .volumeWeek(try container.decode(VolumeWeek.self, forKey: key))
        case .calendarDay:
            self = .calendarDay(try container.decode(CalendarDay.self, forKey: key))
        case .activity:
            self = .activity(try container.decode(ActivitySummary.self, forKey: key))
        case .activityDetail:
            self = .activityDetail(try container.decode(ActivityDetail.self, forKey: key))
        case .stream:
            self = .stream(try container.decode(ActivityStreams.self, forKey: key))
        case .other:
            self = .other(try container.decode(JSONValue.self, forKey: key))
        }
    }

    fileprivate func encode<Key: CodingKey>(
        into container: inout KeyedEncodingContainer<Key>, key: Key
    ) throws {
        switch self {
        case let .profile(value): try container.encode(value, forKey: key)
        case let .trainingState(value): try container.encode(value, forKey: key)
        case let .ftpHistory(value): try container.encode(value, forKey: key)
        case let .loadPoint(value): try container.encode(value, forKey: key)
        case let .curve(value): try container.encode(value, forKey: key)
        case let .volumeWeek(value): try container.encode(value, forKey: key)
        case let .calendarDay(value): try container.encode(value, forKey: key)
        case let .activity(value): try container.encode(value, forKey: key)
        case let .activityDetail(value): try container.encode(value, forKey: key)
        case let .stream(value): try container.encode(value, forKey: key)
        case let .other(value): try container.encode(value, forKey: key)
        case .tombstone: try container.encode(JSONValue.object([:]), forKey: key)
        }
    }
}

// MARK: - The published kinds
//
// Every numeric field is a `Double?` and every string optional, because
// `_safe_data` in snapshot.py turns a non-finite float into `null` and several
// of these values are genuinely absent for a rider with no FTP, no HR data or
// no weight history.  A model that demanded them would decode a real, correct
// snapshot as a failure. Doubles rather than Ints throughout for the same
// reason: `JSONDecoder` reads an integer into a `Double` but refuses a
// fractional number as an `Int`, and which one the server sends depends on
// whether a `round()` happened to land on a whole number.

/// `profile`.  Two publishers emit this kind and they do not agree.
///
/// `snapshot.profile_object` (the walking skeleton's, #171) emits a single
/// `ftp_watts`; `snapshot._profile_object` (the derived snapshot) emits `ftp`
/// plus zones and weight.  Reading both is not defensiveness -- it is what
/// lets the debug round-trip and the real dashboard share one type.
struct RiderProfile: Codable, Sendable, Equatable {
    let displayName: String?
    let ftp: Double?
    let ftpWatts: Double?
    let power: MetricState?
    let heartRate: MetricState?
    let weightKg: Double?
    let weightDate: String?
    let weightSource: String?

    private enum CodingKeys: String, CodingKey {
        case displayName = "display_name"
        case ftp
        case ftpWatts = "ftp_watts"
        case power
        case heartRate = "heart_rate"
        case weightKg = "weight_kg"
        case weightDate = "weight_date"
        case weightSource = "weight_source"
    }

    /// The rider's FTP whichever publisher wrote it, or nil when unpublished.
    var resolvedFTP: Double? { ftp ?? ftpWatts ?? power?.value }
}

/// The `{available, value, source, zones}` block shared by power and HR.
struct MetricState: Codable, Sendable, Equatable {
    let available: Bool
    let value: Double?
    let source: String?
    let zones: [Zone]?
}

struct Zone: Codable, Sendable, Equatable {
    let label: String?
    let name: String?
    let pct: Double?
    let min: Double?
    /// Nil in the open-ended top zone, which is a value, not a gap.
    let max: Double?
    let range: String?
}

/// `training_state`: the numbers the dashboard's header reads.
struct TrainingState: Codable, Sendable, Equatable {
    let ftp: Double?
    let cp: Double?
    let wprime: Double?
    let ctl: Double?
    let atl: Double?
    let tsb: Double?
    let decoupling: Double?
}

/// `ftp_history`: one dated FTP.
struct FTPHistoryPoint: Codable, Sendable, Equatable {
    let date: String?
    let ftpWatts: Double?
    let source: String?

    private enum CodingKeys: String, CodingKey {
        case date
        case ftpWatts = "ftp_watts"
        case source
    }
}

/// `load_point`: one day of the CTL/ATL/TSB series.
struct LoadPoint: Codable, Sendable, Equatable {
    let date: String?
    let tss: Double?
    let ctl: Double?
    let atl: Double?
    let tsb: Double?
}

/// `curve`: mean-maximal power, measured and modelled.
struct PowerCurve: Codable, Sendable, Equatable {
    let measured: [CurvePoint]?
    let allTime: [CurvePoint]?
    let lastRide: [CurvePoint]?
    let model: [CurvePoint]?
    let cp: Double?
    let wprime: Double?

    private enum CodingKeys: String, CodingKey {
        case measured
        case allTime = "all_time"
        case lastRide = "last_ride"
        case model, cp, wprime
    }
}

struct CurvePoint: Codable, Sendable, Equatable {
    /// Duration in seconds.
    let t: Double?
    let power: Double?
}

/// `volume_week`: one Monday-anchored week.
struct VolumeWeek: Codable, Sendable, Equatable {
    let weekStart: String?
    let hours: Double?
    let tss: Double?
    let distanceKm: Double?
    let calories: Double?

    private enum CodingKeys: String, CodingKey {
        case weekStart = "week_start"
        case hours, tss
        case distanceKm = "distance_km"
        case calories
    }
}

/// `calendar_day`.
///
/// `workouts` and `activities` are the desktop's own row shapes, straight out
/// of `db._plan_workout_row`, and `race` is a race row.  They stay as JSON:
/// the calendar screen is #163's, the schema is the desktop's, and a Swift
/// mirror of it here would turn one added column into a decode failure for a
/// whole day.  `part`/`parts` appear only where a day was too large for one
/// object and had to be split.
struct CalendarDay: Codable, Sendable, Equatable {
    let date: String?
    let ooto: Bool?
    let phase: String?
    let race: JSONValue?
    let workouts: [JSONValue]?
    let activities: [JSONValue]?
    let part: Int?
    let parts: Int?
}

/// `activity`: the summary row, which is what a list renders.
struct ActivitySummary: Codable, Sendable, Equatable {
    let id: Double?
    let startTime: String?
    let durationS: Double?
    let distanceM: Double?
    let avgPower: Double?
    let avgHr: Double?
    let np: Double?
    /// `if_` on the wire: `if` is a Python keyword and the column kept the
    /// underscore rather than being renamed on its way out.
    let intensityFactor: Double?
    let tss: Double?
    let rpe: Double?

    private enum CodingKeys: String, CodingKey {
        case id
        case startTime = "start_time"
        case durationS = "duration_s"
        case distanceM = "distance_m"
        case avgPower = "avg_power"
        case avgHr = "avg_hr"
        case np
        case intensityFactor = "if_"
        case tss, rpe
    }
}

/// `activity_detail`: the summary plus what only one ride's page needs.
struct ActivityDetail: Codable, Sendable, Equatable {
    let id: Double?
    let startTime: String?
    let durationS: Double?
    let distanceM: Double?
    let avgPower: Double?
    let avgHr: Double?
    let np: Double?
    let intensityFactor: Double?
    let tss: Double?
    let rpe: Double?
    let weightKg: Double?
    let weightSource: String?
    let weightDate: String?
    /// The zone summary block, kept as JSON for the same reason a calendar
    /// day's workouts are.
    let zones: JSONValue?

    private enum CodingKeys: String, CodingKey {
        case id
        case startTime = "start_time"
        case durationS = "duration_s"
        case distanceM = "distance_m"
        case avgPower = "avg_power"
        case avgHr = "avg_hr"
        case np
        case intensityFactor = "if_"
        case tss, rpe
        case weightKg = "weight_kg"
        case weightSource = "weight_source"
        case weightDate = "weight_date"
        case zones
    }
}

/// `stream`: the downsampled per-second channels for one ride.
///
/// The server downsamples to 1,500 points before publishing, so this is
/// bounded by construction.  A channel is absent when the ride did not record
/// it, and an element is nil where the recording had a gap.
struct ActivityStreams: Codable, Sendable, Equatable {
    let streams: Channels

    struct Channels: Codable, Sendable, Equatable {
        let time: [Double?]?
        let power: [Double?]?
        let heartrate: [Double?]?
        let cadence: [Double?]?
        let altitude: [Double?]?
    }
}

// MARK: - Responses

/// What every collection route returns.
///
/// `revision` and `next_cursor` are present only on the three routes that
/// serve deltas -- `dashboard`, `volume`, `curve`, the ones `api.py` marks
/// `mobile=True`.  On the others the response is `{"items": [...]}` and there
/// is no checkpoint to cache against, which is why `CloudRoute` carries that
/// distinction as a fact about the route rather than inferring it from a
/// response that might merely have been truncated.
struct CollectionResponse: Decodable, Sendable {
    let items: [CloudItem]
    let revision: Int?
    let nextCursor: String?

    private enum CodingKeys: String, CodingKey {
        case items, revision
        case nextCursor = "next_cursor"
    }
}

/// `GET /api/v1/context`: what this reader may read, and where it stands.
struct ContextResponse: Decodable, Sendable {
    let capabilities: [String: Bool]
    let revision: Int
}

/// `POST /api/v1/context/refresh`.
struct RefreshResponse: Decodable, Sendable {
    let readerContext: String
    let expiresIn: Double?
    let capabilities: [String]?

    private enum CodingKeys: String, CodingKey {
        case readerContext = "reader_context"
        case expiresIn = "expires_in"
        case capabilities
    }
}

/// `POST /api/v1/devices/pair`.
struct PairingResponse: Decodable, Sendable {
    let deviceCredential: String
    let deviceSubscriptionKey: String
    let deviceSignatureAlgorithm: String
    let deviceCapabilities: [String]?
    let signingNamespace: String
    let readerContext: String
    let expiresIn: Double?

    private enum CodingKeys: String, CodingKey {
        case deviceCredential = "device_credential"
        case deviceSubscriptionKey = "device_subscription_key"
        case deviceSignatureAlgorithm = "device_signature_algorithm"
        case deviceCapabilities = "device_capabilities"
        case signingNamespace = "signing_namespace"
        case readerContext = "reader_context"
        case expiresIn = "expires_in"
    }
}
