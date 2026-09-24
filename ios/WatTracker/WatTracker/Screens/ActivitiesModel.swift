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
    func refresh(session: (any ReadSession)?) async {
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
        do {
            snapshot = try await session.load(.activities)
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

    var durationText: String { ZoneFormatting.duration(seconds) }
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
