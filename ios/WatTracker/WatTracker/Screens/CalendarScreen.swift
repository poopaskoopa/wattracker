import Foundation
import Observation
import SwiftUI

/// The month view of planned work, completed rides and training context.
///
/// Calendar data is deliberately read through the shared session. That keeps
/// the screen on the same credential and cache as Dashboard and means a
/// cached calendar is useful while the phone is offline.
struct CalendarScreen: View {
    @Environment(\.cloudSession) private var session
    @State private var model = CalendarModel()
    @State private var selectedDay: CalendarDayEntry?

    var body: some View {
        ScreenScaffold(
            title: "Calendar",
            subtitle: "Planned and completed, by day"
        ) {
            content
        }
        .task { await model.start(session: session) }
        .sheet(item: $selectedDay) { day in
            CalendarDayDetail(day: day)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .starting:
            CalendarStatusPanel(
                symbol: "calendar",
                title: "Syncing calendar…",
                message: nil,
                progress: true
            )
        case .unpaired:
            CalendarStatusPanel(
                symbol: "link.badge.plus",
                title: "Pair your desktop",
                message: "Pair this device to see your training calendar.",
                progress: false
            )
        case .removed:
            CalendarStatusPanel(
                symbol: "lock.slash",
                title: "Device removed",
                message: "Pair this device again from the desktop to continue.",
                progress: false
            )
        case .noData:
            CalendarStatusPanel(
                symbol: "calendar.badge.exclamationmark",
                title: "No calendar data yet",
                message: "Your paired desktop has not published calendar data yet.",
                progress: false
            )
        case let .error(message):
            CalendarStatusPanel(
                symbol: "exclamationmark.triangle",
                title: "Calendar unavailable",
                message: message,
                progress: false
            )
        case let .ready(calendar):
            CalendarContent(
                model: model,
                calendar: calendar,
                selectDay: { selectedDay = $0 }
            )
        }
    }
}

@MainActor
@Observable
final class CalendarModel {
    enum State {
        case starting
        case unpaired
        case removed
        case noData
        case error(String)
        case ready(CalendarData)
    }

    private(set) var state: State = .starting
    var month = CalendarMonth.current()

    private var started = false

    func start(session: (any ReadSession)?) async {
        guard !started else { return }
        started = true

        guard let session else {
            state = .unpaired
            return
        }

        switch await session.deviceState {
        case .unpaired:
            state = .unpaired
            return
        case .removed:
            state = .removed
            return
        case .paired:
            break
        }

        // Applying the cache before awaiting the network gives the calendar
        // an immediate first paint and keeps month navigation local.
        let cachedActivities = session.cached(.activities)
        if let cached = session.cached(.calendar) {
            apply(cached, activities: cachedActivities)
        }

        do {
            let calendar = try await session.load(.calendar)
            // Linked rides are intentionally omitted from calendar_day
            // objects to avoid publishing the same activity twice. The
            // activities collection supplies those summaries when it is
            // available; a calendar-only response still renders completed
            // workout status and remains useful offline.
            var activities = cachedActivities
            do {
                activities = try await session.load(.activities)
            } catch let failure as CloudSession.Failure {
                switch failure {
                case .notPaired, .deviceRemoved:
                    // Preserve the session's terminal lifecycle state. A
                    // calendar response arriving just before revocation must
                    // never keep this screen presenting stale rider data.
                    throw failure
                default:
                    activities = cachedActivities
                }
            } catch {
                // A missing activities collection should not hide a usable
                // calendar snapshot.
            }
            apply(calendar, activities: activities ?? cachedActivities)
        } catch let failure as CloudSession.Failure {
            switch failure {
            case .notPaired:
                state = .unpaired
            case .deviceRemoved:
                state = .removed
            default:
                if case .ready = state { return }
                state = .error(failure.description)
            }
        } catch {
            if case .ready = state { return }
            state = .error(error.localizedDescription)
        }
    }

    func moveMonth(by offset: Int) {
        month = month.adding(months: offset)
    }

    private func apply(
        _ snapshot: CloudSnapshot, activities: CloudSnapshot? = nil
    ) {
        let calendar = CalendarData(snapshot: snapshot, activitySnapshot: activities)
        state = calendar.hasContent ? .ready(calendar) : .noData
    }
}

private struct CalendarContent: View {
    @Bindable var model: CalendarModel
    let calendar: CalendarData
    let selectDay: (CalendarDayEntry) -> Void

    private let weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if calendar.source == .cache {
                CalendarCacheBanner(asOf: calendar.asOf)
            }

            Panel {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 8) {
                        Button {
                            model.moveMonth(by: -1)
                        } label: {
                            Image(systemName: "chevron.left")
                                .frame(width: 34, height: 30)
                        }
                        .buttonStyle(.bordered)
                        .tint(Palette.accent)
                        .accessibilityLabel("Previous month")

                        Text(model.month.title)
                            .font(.headline)
                            .foregroundStyle(Palette.textBright)
                            .frame(maxWidth: .infinity)

                        Button {
                            model.moveMonth(by: 1)
                        } label: {
                            Image(systemName: "chevron.right")
                                .frame(width: 34, height: 30)
                        }
                        .buttonStyle(.bordered)
                        .tint(Palette.accent)
                        .accessibilityLabel("Next month")
                    }

                    LazyVGrid(
                        columns: Array(
                            repeating: GridItem(.flexible(minimum: 24), spacing: 4),
                            count: 7
                        ),
                        spacing: 6
                    ) {
                        ForEach(weekdays, id: \.self) { weekday in
                            Text(weekday)
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(Palette.muted)
                                .frame(maxWidth: .infinity)
                                .accessibilityHidden(true)
                        }

                        ForEach(0..<model.month.leadingEmptyDays, id: \.self) { _ in
                            Color.clear
                                .frame(minHeight: 72)
                                .accessibilityHidden(true)
                        }

                        ForEach(1...model.month.dayCount, id: \.self) { day in
                            let entry = calendar.entry(for: model.month, day: day)
                                ?? CalendarDayEntry(
                                    dateISO: model.month.isoDate(day: day),
                                    ooto: false,
                                    phase: nil,
                                    race: nil,
                                    workouts: [],
                                    activities: []
                                )
                            CalendarDayCell(day: day, entry: entry) {
                                selectDay(entry)
                            }
                        }
                    }
                }
            }

            Text("Tap a day for planned workouts and completed rides.")
                .font(.caption)
                .foregroundStyle(Palette.muted)
                .padding(.horizontal, 4)
        }
    }
}

private struct CalendarDayCell: View {
    let day: Int
    let entry: CalendarDayEntry
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            ViewThatFits(in: .horizontal) {
                detailedContent
                compactContent
            }
            .frame(maxWidth: .infinity, minHeight: 72, alignment: .topLeading)
            .padding(7)
            .background(
                entry.ooto ? Palette.surface2 : Palette.panel,
                in: .rect(cornerRadius: 8)
            )
            .overlay {
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(
                        entry.hasContent ? Palette.accent.opacity(0.55) : Palette.surfaceBorder,
                        lineWidth: 1
                    )
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    private var detailedContent: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(String(day))
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                Spacer(minLength: 0)
                if entry.ooto {
                    Image(systemName: "airplane")
                        .font(.caption2)
                        .foregroundStyle(Palette.accent)
                        .accessibilityLabel("Out of office")
                }
                if CalendarJSON.isPresent(entry.race) {
                    Image(systemName: "flag.checkered")
                        .font(.caption2)
                        .foregroundStyle(Palette.alert)
                        .accessibilityLabel("Race")
                }
            }

            if let phase = entry.phase, !phase.isEmpty {
                Text(phase)
                    .font(.caption2)
                    .foregroundStyle(Palette.accent)
                    .lineLimit(1)
            }

            HStack(spacing: 6) {
                if entry.workoutCount > 0 {
                    Label(
                        String(entry.workoutCount),
                        systemImage: "figure.indoor.cycle"
                    )
                    .accessibilityLabel("\(entry.workoutCount) planned workouts")
                }
                if entry.activityCount > 0 {
                    Label(
                        String(entry.activityCount),
                        systemImage: "checkmark.circle"
                    )
                    .accessibilityLabel("\(entry.activityCount) completed rides")
                }
                if CalendarJSON.missedCount(entry.workouts) > 0 {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(Palette.alert)
                        .accessibilityLabel(
                            "\(CalendarJSON.missedCount(entry.workouts)) missed workouts"
                        )
                }
            }
            .font(.caption2)
            .foregroundStyle(Palette.muted)
            .lineLimit(1)
        }
    }

    private var compactContent: some View {
        VStack(spacing: 4) {
            Text(String(day))
                .font(.caption.weight(.semibold))
                .foregroundStyle(Palette.textBright)
            if entry.hasContent {
                Circle()
                    .fill(entry.ooto ? Palette.accent : Palette.ok)
                    .frame(width: 5, height: 5)
                    .accessibilityHidden(true)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var accessibilityLabel: String {
        var parts = ["Day \(day)"]
        if let phase = entry.phase, !phase.isEmpty { parts.append(phase) }
        if entry.ooto { parts.append("out of office") }
        if CalendarJSON.isPresent(entry.race) { parts.append("race") }
        if entry.workoutCount > 0 {
            parts.append("\(entry.workoutCount) planned workouts")
        }
        if entry.activityCount > 0 {
            parts.append("\(entry.activityCount) completed rides")
        }
        return parts.joined(separator: ", ")
    }
}

private struct CalendarDayDetail: View {
    let day: CalendarDayEntry

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Panel {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(CalendarJSON.displayDate(day.dateISO))
                                .font(.headline)
                                .foregroundStyle(Palette.textBright)
                            if day.ooto {
                                Label("Out of office", systemImage: "airplane")
                                    .foregroundStyle(Palette.accent)
                            }
                            if let phase = day.phase, !phase.isEmpty {
                                detailRow("Training phase", phase)
                            }
                            if let race = day.race, CalendarJSON.isPresent(race) {
                                detailRow("Race", CalendarJSON.raceTitle(race))
                            }
                        }
                    }

                    if !day.workouts.isEmpty {
                        CalendarWorkoutPanel(workouts: day.workouts)
                    }

                    if !day.activities.isEmpty {
                        Panel {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Completed rides")
                                    .font(.headline)
                                    .foregroundStyle(Palette.textBright)
                                ForEach(
                                    Array(day.activities.enumerated()),
                                    id: \.offset
                                ) { _, activity in
                                    NavigationLink {
                                        CalendarActivityDetail(activity: activity)
                                    } label: {
                                        CalendarActivityRow(activity: activity)
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                    }

                    if day.workouts.isEmpty && day.activities.isEmpty {
                        Panel {
                            Text("Nothing planned or recorded on this day.")
                                .font(.callout)
                                .foregroundStyle(Palette.muted)
                        }
                    }
                }
                .padding(16)
            }
            .background(Palette.bg)
            .navigationTitle("Calendar day")
            .navigationBarTitleDisplayMode(.inline)
        }
        .preferredColorScheme(.dark)
    }

    private func detailRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .foregroundStyle(Palette.muted)
            Spacer(minLength: 12)
            Text(value)
                .foregroundStyle(Palette.text)
                .multilineTextAlignment(.trailing)
        }
        .font(.callout)
    }
}

private struct CalendarWorkoutPanel: View {
    let workouts: [JSONValue]

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 8) {
                Text("Planned workouts")
                    .font(.headline)
                    .foregroundStyle(Palette.textBright)
                ForEach(Array(workouts.enumerated()), id: \.offset) { index, workout in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(CalendarJSON.title(workout, fallback: "Workout"))
                                .font(.callout.weight(.semibold))
                                .foregroundStyle(Palette.text)
                            Spacer(minLength: 6)
                            Text(CalendarJSON.workoutStatus(workout))
                                .font(.caption)
                                .foregroundStyle(CalendarJSON.statusColor(workout))
                        }
                        HStack(spacing: 10) {
                            if let type = CalendarJSON.string(workout, keys: ["type"]), !type.isEmpty {
                                Text(type)
                            }
                            if let duration = CalendarJSON.duration(workout) {
                                Text(duration)
                            }
                            if let tss = CalendarJSON.number(workout, keys: ["tss"]) {
                                Text("TSS \(CalendarJSON.numberString(tss))")
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                    }
                    if index < workouts.count - 1 {
                        Divider().overlay(Palette.surfaceBorder)
                    }
                }
            }
        }
    }
}

private struct CalendarActivityRow: View {
    let activity: JSONValue

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "figure.outdoor.cycle")
                .foregroundStyle(Palette.ok)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(CalendarJSON.title(activity, fallback: "Recorded ride"))
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Palette.text)
                HStack(spacing: 8) {
                    if let start = CalendarJSON.string(activity, keys: ["start_time"]) {
                        Text(CalendarJSON.displayDateTime(start))
                    }
                    if let duration = CalendarJSON.duration(activity) {
                        Text(duration)
                    }
                    if let distance = CalendarJSON.number(activity, keys: ["distance_m"]) {
                        Text(CalendarJSON.distanceString(distance))
                    }
                }
                .font(.caption)
                .foregroundStyle(Palette.muted)
            }
            Spacer(minLength: 4)
            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Palette.muted)
                .accessibilityHidden(true)
        }
        .padding(.vertical, 5)
        .contentShape(Rectangle())
    }
}

private struct CalendarActivityDetail: View {
    let activity: JSONValue

    private var metrics: [CalendarMetric] {
        [
            CalendarJSON.number(activity, keys: ["duration_s"]).map {
                CalendarMetric(label: "Duration", value: CalendarJSON.durationString($0))
            },
            CalendarJSON.number(activity, keys: ["distance_m"]).map {
                CalendarMetric(label: "Distance", value: CalendarJSON.distanceString($0))
            },
            CalendarJSON.number(activity, keys: ["avg_power"]).map {
                CalendarMetric(label: "Average power", value: "\(CalendarJSON.numberString($0)) W")
            },
            CalendarJSON.number(activity, keys: ["np"]).map {
                CalendarMetric(label: "Normalized power", value: "\(CalendarJSON.numberString($0)) W")
            },
            CalendarJSON.number(activity, keys: ["avg_hr"]).map {
                CalendarMetric(label: "Average heart rate", value: "\(CalendarJSON.numberString($0)) bpm")
            },
            CalendarJSON.number(activity, keys: ["tss"]).map {
                CalendarMetric(label: "TSS", value: CalendarJSON.numberString($0))
            },
            CalendarJSON.number(activity, keys: ["rpe"]).map {
                CalendarMetric(label: "RPE", value: CalendarJSON.numberString($0))
            },
        ].compactMap { $0 }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Panel {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(CalendarJSON.title(activity, fallback: "Recorded ride"))
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(Palette.textBright)
                        if let start = CalendarJSON.string(activity, keys: ["start_time"]) {
                            Text(CalendarJSON.displayDateTime(start))
                                .font(.callout)
                                .foregroundStyle(Palette.muted)
                        }
                    }
                }
                if !metrics.isEmpty {
                    Panel {
                        VStack(spacing: 0) {
                            ForEach(metrics) { metric in
                                HStack(alignment: .firstTextBaseline) {
                                    Text(metric.label)
                                        .foregroundStyle(Palette.muted)
                                    Spacer(minLength: 12)
                                    Text(metric.value)
                                        .foregroundStyle(Palette.textBright)
                                        .monospacedDigit()
                                }
                                .font(.callout)
                                .padding(.vertical, 7)
                                if metric.id != metrics.last?.id {
                                    Divider().overlay(Palette.surfaceBorder)
                                }
                            }
                        }
                    }
                }
            }
            .padding(16)
        }
        .background(Palette.bg)
        .navigationTitle("Ride detail")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct CalendarMetric: Identifiable {
    let id = UUID()
    let label: String
    let value: String
}

private struct CalendarStatusPanel: View {
    let symbol: String
    let title: String
    let message: String?
    let progress: Bool

    var body: some View {
        Panel {
            HStack(alignment: .top, spacing: 12) {
                if progress {
                    ProgressView()
                        .tint(Palette.accent)
                } else {
                    Image(systemName: symbol)
                        .font(.title2)
                        .foregroundStyle(Palette.accent)
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text(title)
                        .font(.headline)
                        .foregroundStyle(Palette.textBright)
                    if let message {
                        Text(message)
                            .font(.callout)
                            .foregroundStyle(Palette.muted)
                    }
                }
            }
        }
    }
}

private struct CalendarCacheBanner: View {
    let asOf: Date

    var body: some View {
        Label {
            Text(
                "Cached data · last synced "
                    + asOf.formatted(date: .abbreviated, time: .shortened)
            )
        } icon: {
            Image(systemName: "arrow.clockwise.circle")
        }
        .font(.caption)
        .foregroundStyle(Palette.accent)
        .padding(.horizontal, 4)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "Showing cached calendar data. Last synced "
                + asOf.formatted(date: .abbreviated, time: .shortened)
        )
    }
}

private enum CalendarJSON {
    static func value(_ object: JSONValue, key: String) -> JSONValue? {
        object[key]
    }

    static func string(_ object: JSONValue, keys: [String]) -> String? {
        for key in keys {
            if let value = value(object, key: key)?.stringValue, !value.isEmpty {
                return value
            }
        }
        return nil
    }

    static func number(_ object: JSONValue, keys: [String]) -> Double? {
        for key in keys {
            if let value = value(object, key: key)?.doubleValue, value.isFinite {
                return value
            }
        }
        return nil
    }

    static func bool(_ object: JSONValue, key: String) -> Bool {
        if case let .some(.bool(value)) = value(object, key: key) { return value }
        return false
    }

    static func isPresent(_ value: JSONValue?) -> Bool {
        guard let value else { return false }
        if case .null = value { return false }
        return true
    }

    static func title(_ object: JSONValue, fallback: String) -> String {
        string(object, keys: ["name", "title", "workout_name"]) ?? fallback
    }

    static func missedCount(_ workouts: [JSONValue]) -> Int {
        workouts.reduce(into: 0) { count, workout in
            if bool(workout, key: "missed") || bool(workout, key: "skipped") {
                count += 1
            }
        }
    }

    static func workoutStatus(_ workout: JSONValue) -> String {
        if isPresent(value(workout, key: "completed_activity_id")) {
            return "Completed"
        }
        if bool(workout, key: "missed") { return "Missed" }
        if bool(workout, key: "skipped") { return "Skipped" }
        if bool(workout, key: "adapted") { return "Adapted" }
        return "Planned"
    }

    static func statusColor(_ workout: JSONValue) -> Color {
        switch workoutStatus(workout) {
        case "Completed": return Palette.ok
        case "Missed", "Skipped": return Palette.alert
        case "Adapted": return Palette.accent
        default: return Palette.muted
        }
    }

    static func duration(_ object: JSONValue) -> String? {
        number(object, keys: ["duration_s"]).map(durationString)
    }

    static func durationString(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "—" }
        let total = Int(seconds.rounded())
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(minutes)m"
    }

    static func distanceString(_ metres: Double) -> String {
        guard metres.isFinite, metres >= 0 else { return "—" }
        return "\(numberString(metres / 1000)) km"
    }

    static func numberString(_ value: Double) -> String {
        guard value.isFinite else { return "—" }
        let formatted = String(format: "%.1f", value)
        return formatted.hasSuffix(".0") ? String(formatted.dropLast(2)) : formatted
    }

    static func raceTitle(_ race: JSONValue) -> String {
        title(race, fallback: "Race")
    }

    static func displayDate(_ value: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale.current
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "EEEE, MMM d, yyyy"
        return dateOnly(value).map(formatter.string(from:)) ?? value
    }

    static func displayDateTime(_ value: String) -> String {
        if let date = instant(value) {
            return date.formatted(date: .abbreviated, time: .shortened)
        }
        return displayDate(value)
    }

    private static func dateOnly(_ value: String) -> Date? {
        guard value.count == 10 else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.date(from: value)
    }

    private static func instant(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
}
