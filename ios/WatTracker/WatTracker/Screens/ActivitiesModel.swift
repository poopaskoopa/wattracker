import Foundation
import Observation
import SwiftUI

/// What the activities list is allowed to show, and the reads that decide it.
///
/// The screen used to hold `snapshot` + `errorMessage` in `@State` and funnel
/// every failure into the message. That cannot express "stop showing this
/// rider's rides": a revoke observed during a pull-to-refresh wiped the
/// credential and the cache inside the session, but the already-rendered
/// snapshot stayed in the view, so the revoked rider's full ride list kept
/// rendering with an error line above it until the app was backgrounded. The
/// terminal states clear the data here, the way `DashboardModel` does.
@MainActor
@Observable
final class ActivitiesModel {
    enum Availability: Equatable {
        case available
        case unpaired
        case removed
    }

    private(set) var snapshot: CloudSnapshot?
    private(set) var availability: Availability = .available
    private(set) var errorMessage: String?
    private(set) var isLoading = false
    var selectedID: String?
    let wake = CloudWakeNotice()

    var rides: [RideSummary] {
        (snapshot?.items ?? []).compactMap(RideSummary.init(item:)).sorted {
            if $0.startedAt != $1.startedAt { return $0.startedAt > $1.startedAt }
            return $0.id > $1.id
        }
    }

    /// Takes the session rather than building one.
    ///
    /// This screen used to build its own when none was passed in. That
    /// predates the gate: two sessions over one keychain credential means a
    /// sign-out or a revoke performed in Settings leaves this screen holding
    /// one that still believes it is paired, listing a signed-out rider's
    /// rides. The signing-key failure the old initialiser reported as
    /// `startupError` is the gate's to report now -- it never gets as far as
    /// showing this screen.
    ///
    /// `userInitiated` is pull to refresh: the rider asked, so a backoff left
    /// by the cloud waking up is lifted rather than making them wait it out.
    func refresh(session: (any ReadSession)?, userInitiated: Bool = false) async {
        // No session means the gate has not produced one: no credential yet,
        // or the signing key could not be read. Neither is a ride list.
        guard let session else {
            clear(.unpaired, message: CloudSession.Failure.notPaired.description)
            return
        }

        switch await session.deviceState {
        case .unpaired:
            clear(.unpaired, message: CloudSession.Failure.notPaired.description)
            return
        case .removed:
            clear(.removed, message: CloudSession.Failure.deviceRemoved.description)
            return
        case .paired:
            break
        }

        // First paint from cache before the network, the way the Dashboard
        // does it. In the view's `init` previously; the injected session is
        // not readable there.
        if snapshot == nil { snapshot = session.cached(.activities) }
        isLoading = true
        defer { isLoading = false }
        if userInitiated { await session.retryNowIfWaking() }
        do {
            snapshot = try await wake.during(session) { try await session.load(.activities) }
            availability = .available
            errorMessage = nil
            if selectedID == nil { selectedID = rides.first?.id }
        } catch let failure as CloudSession.Failure {
            switch failure {
            case .notPaired:
                clear(.unpaired, message: failure.description)
            case .deviceRemoved:
                clear(.removed, message: failure.description)
            default:
                // A cached, usable list stays visible when the refresh fails
                // for an ordinary network reason -- the session normally
                // returns it as `.cache` anyway. Revocation and unpairing are
                // handled above so they always clear the view.
                errorMessage = failure.description
            }
        } catch {
            errorMessage = String(describing: error)
        }
    }

    /// Leave nothing of the previous rider's rides behind.
    private func clear(_ availability: Availability, message: String) {
        self.availability = availability
        snapshot = nil
        selectedID = nil
        errorMessage = message
    }
}

struct RideSummary: Identifiable {
    let id: String
    let activityID: Int
    let summary: ActivitySummary
    let startedAt: String

    init?(item: CloudItem) {
        guard !item.deleted, case let .activity(summary) = item.payload else { return nil }
        let numericID = summary.id.map(Int.init)
            ?? Int(item.id.split(separator: "-").last ?? "")
        guard let numericID else { return nil }
        id = item.id
        activityID = numericID
        self.summary = summary
        startedAt = summary.startTime ?? ""
    }
}

struct StreamPoint: Identifiable { let id: Int; let time: Double; let value: Double }

struct StreamSeries: Identifiable {
    let id: String
    let title: String
    let color: Color
    let points: [StreamPoint]

    static func all(in payload: ActivityStreams) -> [StreamSeries] {
        let channels = payload.streams
        return [
            make("power", "Power (W)", Palette.accent, channels.power, channels.time),
            make("heart-rate", "Heart rate (bpm)", Palette.hr, channels.heartrate, channels.time),
            make("cadence", "Cadence (rpm)", Palette.ok, channels.cadence, channels.time),
            make("altitude", "Altitude (m)", .cyan, channels.altitude, channels.time),
        ].compactMap { $0 }
    }

    private static func make(
        _ id: String, _ title: String, _ color: Color,
        _ values: [Double?]?, _ times: [Double?]?
    ) -> StreamSeries? {
        guard let values else { return nil }
        let points = values.enumerated().compactMap { index, value -> StreamPoint? in
            guard let value, value.isFinite else { return nil }
            let time: Double
            if let times, index < times.count, let candidate = times[index], candidate.isFinite {
                time = candidate
            } else { time = Double(index) }
            return StreamPoint(id: index, time: time, value: value)
        }
        guard !points.isEmpty else { return nil }
        return StreamSeries(id: id, title: title, color: color, points: points)
    }
}

/// A y-axis of the combined ride chart: real-unit ticks over `lower...upper`.
struct ChartAxis: Equatable {
    let lower: Double
    let upper: Double
    let ticks: [Double]
    let title: String

    /// Tick positions on the shared 0...1 plot band.
    var scaledTicks: [Double] { ticks.map { ($0 - lower) / (upper - lower) } }

    /// Real value at a 0...1 plot position (inverse of the series scaling).
    func value(atScaled position: Double) -> Double { lower + position * (upper - lower) }

    /// Zero-based "nice" axis covering `values` (negatives extend the floor).
    static func zeroBased(for values: [Double], title: String) -> ChartAxis {
        let hi = values.max() ?? 0, lo = min(0, values.min() ?? 0)
        let (ticks, lower, upper) = ChartTicks.nice(lower: lo, upper: hi, target: 5)
        return ChartAxis(lower: lower, upper: upper, ticks: ticks, title: title)
    }
}

enum ChartTicks {
    /// Round tick values with 1/2/5 x 10^n steps; the returned bounds enclose
    /// `lower...upper` and always span a positive range.
    static func nice(lower: Double, upper: Double, target: Int) -> (ticks: [Double], lower: Double, upper: Double) {
        let hi = max(upper, lower), span = hi - lower
        let rough = (span > 1e-9 ? span : 1) / Double(max(target, 1))
        let magnitude = pow(10, floor(log10(rough)))
        let step = [1.0, 2, 5, 10].map { $0 * magnitude }.first { $0 >= rough } ?? 10 * magnitude
        let lo = (lower / step).rounded(.down) * step
        var top = (hi / step).rounded(.up) * step
        if top <= lo { top = lo + step }
        let count = Int(((top - lo) / step).rounded())
        return ((0...count).map { lo + Double($0) * step }, lo, top)
    }

    /// Minute ticks for 0...`minutes`: whole-minute steps, at most ~6 labels.
    static func minutes(upTo minutes: Double) -> [Double] {
        guard minutes.isFinite, minutes > 0 else { return [0] }
        let step = [1.0, 2, 5, 10, 15, 20, 30, 60, 120, 240].first { minutes / $0 <= 6 } ?? 480
        return Array(stride(from: 0.0, through: minutes, by: step))
    }
}

/// One series of the combined ride chart, rescaled onto a shared 0...1 band.
/// Mirrors the desktop chart (wattracker/web/static/app.js, renderActivityDetail):
/// power has its own left axis, heart rate and cadence share one right axis
/// (both zero-based), and altitude is a faint filled background on an auto
/// (min...max) scale with no axis.
struct CombinedChartSeries: Identifiable {
    struct Point: Identifiable { let id: Int; let minutes: Double; let value: Double; let scaled: Double }
    let id: String
    let title: String
    let color: Color
    let isBackground: Bool
    let lower: Double
    let upper: Double
    let points: [Point]

    /// Desktop draws altitude as fill 0.18 / border 0.5 of the context colour.
    static let altitudeFillOpacity = 0.18
    static let altitudeLineOpacity = 0.5

    /// Altitude first (drawn behind), then power, heart rate, cadence.
    static func build(from payload: ActivityStreams, durationSeconds: Double? = nil) -> [CombinedChartSeries] {
        let all = StreamSeries.all(in: payload)
        let order = ["altitude", "power", "heart-rate", "cadence"]
        let clock = MinutesClock(payload, durationSeconds: durationSeconds)
        let powerAxis = axis(.power, in: all)
        let sharedAxis = axis(.shared, in: all)
        return order.compactMap { id in
            guard let source = all.first(where: { $0.id == id }) else { return nil }
            let isAltitude = id == "altitude"
            let values = source.points.map(\.value)
            let lo: Double, hi: Double
            if isAltitude {
                lo = values.min() ?? 0; hi = values.max() ?? 0
            } else {
                let a = id == "power" ? powerAxis : sharedAxis
                lo = a?.lower ?? 0; hi = a?.upper ?? 1
            }
            let span = hi - lo
            let points = source.points.map { p in
                Point(id: p.id, minutes: clock.minutes(for: p), value: p.value,
                      scaled: span > 0 ? (p.value - lo) / span : 0.5)
            }
            return CombinedChartSeries(
                id: id, title: source.title,
                color: isAltitude ? Palette.muted : source.color,
                isBackground: isAltitude, lower: lo, upper: hi, points: points)
        }
    }

    enum Side { case power, shared }

    /// Left (power, "W") or right (heart rate + cadence, "bpm / rpm") axis,
    /// built from every series on that side whether or not it is hidden, so
    /// toggling the legend never rescales the plot.
    static func axis(_ side: Side, in all: [StreamSeries]) -> ChartAxis? {
        let ids: Set<String> = side == .power ? ["power"] : ["heart-rate", "cadence"]
        let values = all.filter { ids.contains($0.id) }.flatMap { $0.points.map(\.value) }
        guard !values.isEmpty else { return nil }
        return .zeroBased(for: values, title: side == .power ? "W" : "bpm / rpm")
    }

    /// X-axis extent in minutes (0...ride length) for a built chart.
    static func extent(_ series: [CombinedChartSeries]) -> Double {
        series.flatMap(\.points).map(\.minutes).max() ?? 0
    }
}

/// Converts a stream sample to true elapsed minutes.
///
/// The cloud's `time` channel is usually all nil: the desktop stores ISO
/// timestamps and the publisher's numeric downsampler drops them. Falling back
/// to the sample index (as this once did) labelled a 1,500-point downsample of
/// an 86-minute ride 0...25 minutes. With the ride duration known, samples are
/// spread evenly over 0...duration (the downsample is uniform); without it, 1 Hz
/// is assumed.
struct MinutesClock {
    private let hasTime: Bool
    private let count: Int
    private let durationSeconds: Double?

    init(_ payload: ActivityStreams, durationSeconds: Double?) {
        let c = payload.streams
        hasTime = (c.time ?? []).contains { $0?.isFinite == true }
        count = [c.power, c.heartrate, c.cadence, c.altitude, c.time]
            .map { $0?.count ?? 0 }.max() ?? 0
        self.durationSeconds = durationSeconds.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
    }

    func minutes(for point: StreamPoint) -> Double {
        if hasTime { return point.time / 60 }
        if let durationSeconds, count > 1 {
            return Double(point.id) / Double(count - 1) * durationSeconds / 60
        }
        return Double(point.id) / 60
    }
}

struct ZoneGroup: Identifiable {
    let id: String
    let title: String
    let rows: [ZoneRow]

    static func extract(from value: JSONValue?) -> [ZoneGroup] {
        [("power", "Power zones"), ("heart_rate", "Heart-rate zones")].compactMap {
            (key, title) -> ZoneGroup? in
            guard case let .array(values)? = value?[key]?["zones"] else { return nil }
            let rows = values.enumerated().compactMap { index, value -> ZoneRow? in
                guard let seconds = value["seconds"]?.doubleValue, seconds > 0 else {
                    return nil
                }
                return ZoneRow(
                    id: "\(key)-\(index)",
                    label: value["label"]?.stringValue ?? "Z\(index + 1)",
                    seconds: seconds,
                    percent: value["percent"]?.doubleValue ?? 0,
                    duration: value["duration"]?.stringValue
                )
            }
            return rows.isEmpty ? nil : ZoneGroup(id: key, title: title, rows: rows)
        }
    }
}

struct ZoneRow: Identifiable {
    let id: String
    let label: String
    let seconds: Double
    let percent: Double
    let duration: String?

    init(id: String, label: String, seconds: Double, percent: Double, duration: String? = nil) {
        self.id = id
        self.label = label
        self.seconds = seconds
        self.percent = percent
        self.duration = duration
    }

    var durationText: String { duration ?? ZoneFormatting.duration(seconds) }
}

enum ZoneFormatting {
    // Mirrors wattracker/analysis/zones.py:55-59: zone minutes are not padded.
    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "—" }
        let total = Int(seconds.rounded(.toNearestOrEven)), hours = total / 3_600
        let minutes = (total % 3_600) / 60, remainder = total % 60
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, remainder)
            : String(format: "%d:%02d", minutes, remainder)
    }
}

enum RideFormatting {
    static func date(_ value: String?) -> String {
        guard let value else { return "Unknown date" }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let parsed = fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value)
        guard let parsed else { return value.replacingOccurrences(of: "T", with: " ") }
        return parsed.formatted(date: .abbreviated, time: .shortened)
    }

    static func relative(_ date: Date) -> String {
        RelativeDateTimeFormatter().localizedString(for: date, relativeTo: Date())
    }

    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "—" }
        let total = Int(seconds.rounded(.toNearestOrEven)), hours = total / 3_600
        let minutes = (total % 3_600) / 60, remainder = total % 60
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, remainder)
            : String(format: "%02d:%02d", minutes, remainder)
    }

    static func distance(_ meters: Double?) -> String {
        guard let meters, meters.isFinite else { return "—" }
        return String(format: "%.1f km", meters / 1_000)
    }

    static func watts(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "—" }
        return "\(Int(value.rounded())) W"
    }

    static func decimal(_ value: Double?, _ places: Int) -> String {
        guard let value, value.isFinite else { return "—" }
        return String(format: "%.*f", places, value)
    }
}
