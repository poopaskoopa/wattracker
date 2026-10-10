import Foundation
import Charts
import Observation
import SwiftUI

/// The month view of planned work, completed rides and training context.
///
/// Calendar data is deliberately read through the shared session. That keeps
/// the screen on the same credential and cache as Dashboard and means a
/// cached calendar is useful while the phone is offline.
struct CalendarScreen: View {
    @Environment(SessionGate.self) private var gate
    @State private var model = CalendarModel()
    @State private var selectedDay: CalendarDayEntry?

    var body: some View {
        ScreenScaffold(
            title: "Calendar",
            subtitle: "Planned and completed, by day"
        ) {
            content
        }
        .task(id: gate.backend) { await model.start(session: gate.activeSession) }
        .refreshable {
            await model.start(session: gate.activeSession, userInitiated: true)
        }
        .sheet(item: $selectedDay) { day in
            CalendarDayDetail(day: day)
        }
    }

    @ViewBuilder
    private var content: some View {
        if model.wake.isShowing {
            CloudWakingPanel(showingCache: model.isReady)
        }
        switch model.state {
        case .starting:
            if !model.wake.isShowing {
                CalendarStatusPanel(
                    symbol: "calendar",
                    title: "Syncing calendar…",
                    message: nil,
                    progress: true
                )
            }
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
    let wake = CloudWakeNotice()

    var isReady: Bool {
        if case .ready = state { return true }
        return false
    }

    private var requestGeneration = 0

    func start(session: (any ReadSession)?, userInitiated: Bool = false) async {
        let generation = beginRequest()
        state = .starting
        currentFTP = nil

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

        guard generation == requestGeneration else { return }

        // Applying the cache before awaiting the network gives the calendar
        // an immediate first paint while retaining offline use.
        let cachedActivities = session.cached(.activities)
        let cachedDashboard = session.cached(.dashboard)
        if let cached = session.cached(.calendar, month: month) {
            apply(
                cached,
                activities: cachedActivities,
                ftp: CalendarData.currentFTP(from: cachedDashboard),
                session: session
            )
        }

        if userInitiated { await session.retryNowIfWaking() }
        do {
            let calendar = try await wake.during(session) {
                try await session.load(.calendar, month: month)
            }
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
            var dashboard = cachedDashboard
            do {
                dashboard = try await session.load(.dashboard)
            } catch let failure as CloudSession.Failure {
                switch failure {
                case .notPaired, .deviceRemoved:
                    throw failure
                default:
                    break
                }
            } catch {
                // A dashboard outage must not hide a usable calendar.
            }
            guard generation == requestGeneration else { return }
            apply(
                calendar,
                activities: activities ?? cachedActivities,
                ftp: CalendarData.currentFTP(from: dashboard),
                session: session
            )
        } catch let failure as CloudSession.Failure {
            guard generation == requestGeneration else { return }
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
            guard generation == requestGeneration else { return }
            if case .ready = state { return }
            state = .error(error.localizedDescription)
        }
    }

    func moveMonth(by offset: Int, session: (any ReadSession)?) async {
        let selectedMonth = month.adding(months: offset)
        month = selectedMonth
        let generation = beginRequest()
        guard let session else {
            state = .unpaired
            return
        }
        do {
            let calendar = try await wake.during(session) {
                try await session.load(.calendar, month: selectedMonth)
            }
            guard generation == requestGeneration, selectedMonth == month else { return }
            var activities = session.cached(.activities)
            do {
                activities = try await session.load(.activities)
            } catch let failure as CloudSession.Failure {
                switch failure {
                case .notPaired, .deviceRemoved: throw failure
                default: break
                }
            } catch {
                // Calendar data remains useful when the activity list is offline.
            }
            guard generation == requestGeneration, selectedMonth == month else { return }
            apply(calendar, activities: activities, ftp: currentFTP, session: session)
        } catch let failure as CloudSession.Failure {
            guard generation == requestGeneration, selectedMonth == month else { return }
            switch failure {
            case .notPaired: state = .unpaired
            case .deviceRemoved: state = .removed
            default:
                if case .ready = state { return }
                state = .error(failure.description)
            }
        } catch {
            guard generation == requestGeneration, selectedMonth == month else { return }
            if case .ready = state { return }
            state = .error(error.localizedDescription)
        }
    }

    private func beginRequest() -> Int {
        requestGeneration += 1
        return requestGeneration
    }

    private var currentFTP: Double?

    private func apply(
        _ snapshot: CloudSnapshot,
        activities: CloudSnapshot? = nil,
        ftp: Double? = nil,
        session: any ReadSession
    ) {
        currentFTP = ftp
        // The grid's leading and trailing cells are the adjacent months' real
        // dates; whatever the cache already holds for them is shown too.
        let gridSnapshot = CalendarData.gridSnapshot(
            snapshot,
            month: month,
            adjacent: [
                session.cached(.calendar, month: month.adding(months: -1)),
                session.cached(.calendar, month: month.adding(months: 1)),
            ]
        )
        let calendar = CalendarData(
            snapshot: gridSnapshot,
            activitySnapshot: activities,
            ftp: currentFTP
        )
        // Whether the month has data is still decided by the month alone.
        let hasContent = CalendarData(snapshot: snapshot).hasContent
        state = hasContent ? .ready(calendar) : .noData
    }
}

private struct CalendarContent: View {
    @Bindable var model: CalendarModel
    let calendar: CalendarData
    let selectDay: (CalendarDayEntry) -> Void
    @Environment(SessionGate.self) private var gate

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
                            Task { await model.moveMonth(by: -1, session: gate.activeSession) }
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
                            Task { await model.moveMonth(by: 1, session: gate.activeSession) }
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

                        // One ForEach keyed by ISO date: every cell's identity
                        // is unique. Separate blank and day ForEaches keyed by
                        // Int collided (blank 1 vs day 1) and SwiftUI reused
                        // or dropped cells, shifting the month's weekdays.
                        ForEach(model.month.gridDates(), id: \.isoDate) { gridDay in
                            let entry = calendar.entry(for: gridDay.isoDate)
                                ?? CalendarDayEntry(
                                    dateISO: gridDay.isoDate,
                                    ooto: false,
                                    phase: nil,
                                    race: nil,
                                    workouts: [],
                                    activities: [],
                                    currentFTP: calendar.currentFTP
                                )
                            CalendarDayCell(
                                day: gridDay.day,
                                isInMonth: gridDay.isInMonth,
                                entry: entry
                            ) {
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
    let isInMonth: Bool
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
            .background(background, in: .rect(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(
                        entry.hasContent
                            ? Palette.accent.opacity(isInMonth ? 0.55 : 0.3)
                            : Palette.surfaceBorder,
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
                    .foregroundStyle(isInMonth ? Palette.textBright : Palette.muted)
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
                .foregroundStyle(isInMonth ? Palette.textBright : Palette.muted)
            if entry.hasContent {
                Circle()
                    .fill(entry.ooto ? Palette.accent : Palette.ok)
                    .frame(width: 5, height: 5)
                    .accessibilityHidden(true)
            }
        }
        .frame(maxWidth: .infinity)
    }

    /// Adjacent-month cells recede to the inset surface so the month's own
    /// days lead; an out-of-office day keeps its tint, only dimmer.
    private var background: Color {
        if entry.ooto { return Palette.surface2.opacity(isInMonth ? 1 : 0.5) }
        return isInMonth ? Palette.panel : Palette.surfaceInset
    }

    private var accessibilityLabel: String {
        var parts = [isInMonth ? "Day \(day)" : "\(entry.dateISO), adjacent month"]
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
    @Environment(\.dismiss) private var dismiss

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
                        CalendarWorkoutPanel(
                            workouts: day.workouts,
                            ftp: day.currentFTP
                        )
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
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        dismiss()
                    } label: {
                        Image(systemName: "chevron.left")
                    }
                    .accessibilityLabel("Back to calendar")
                }
            }
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
    let ftp: Double?

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
                        let profile = CalendarJSON.profile(workout)
                        if !profile.isEmpty {
                            if let ftp, ftp.isFinite, ftp > 0 {
                                Text(
                                    "at FTP \(Int(ftp.rounded(.toNearestOrEven))) W"
                                )
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Palette.accent)
                                .accessibilityIdentifier("calendar-workout-ftp")
                            }
                            CalendarWorkoutProfileChart(profile: profile, ftp: ftp)
                                .frame(minHeight: 190)
                            VStack(alignment: .leading, spacing: 5) {
                                ForEach(
                                    Array(
                                        CalendarWorkoutFormatting.stepDescriptions(
                                            profile, ftp: ftp
                                        ).enumerated()
                                    ),
                                    id: \.offset
                                ) { _, description in
                                    Text(description)
                                        .font(.callout.monospacedDigit())
                                        .foregroundStyle(Palette.text)
                                }
                                ForEach(
                                    Array(
                                        CalendarWorkoutFormatting.coachingTexts(profile)
                                            .enumerated()
                                    ),
                                    id: \.offset
                                ) { _, text in
                                    Text(text)
                                        .font(.caption)
                                        .foregroundStyle(Palette.muted)
                                }
                            }
                            .accessibilityIdentifier("calendar-workout-steps")
                        }
                    }
                    if index < workouts.count - 1 {
                        Divider().overlay(Palette.surfaceBorder)
                    }
                }
            }
        }
    }
}

private struct CalendarWorkoutProfileChart: View {
    let profile: [CalendarWorkoutProfileBlock]
    let ftp: Double?

    var body: some View {
        let hasFTP = ftp?.isFinite == true && (ftp ?? 0) > 0
        Chart {
            ForEach(profile) { step in
                if !step.free,
                   let start = CalendarWorkoutFormatting.chartTarget(
                       step.targetStart, ftp: ftp
                   ),
                   let end = CalendarWorkoutFormatting.chartTarget(
                       step.targetEnd, ftp: ftp
                   ) {
                    let opacity = CalendarWorkoutFormatting.zoneOpacity(
                        ((step.targetStart ?? 0) + (step.targetEnd ?? 0)) / 2
                    )
                    AreaMark(
                        x: .value("Time", step.start),
                        yStart: .value("Baseline", 0.0),
                        yEnd: .value("Power", start),
                        series: .value("Step", step.id)
                    )
                    .foregroundStyle(Palette.accent.opacity(opacity))
                    AreaMark(
                        x: .value("Time", step.end),
                        yStart: .value("Baseline", 0.0),
                        yEnd: .value("Power", end),
                        series: .value("Step", step.id)
                    )
                    .foregroundStyle(Palette.accent.opacity(opacity))
                    LineMark(
                        x: .value("Time", step.start),
                        y: .value("Power", start),
                        series: .value("Step", step.id)
                    )
                    .foregroundStyle(Palette.accent)
                    LineMark(
                        x: .value("Time", step.end),
                        y: .value("Power", end),
                        series: .value("Step", step.id)
                    )
                    .foregroundStyle(Palette.accent)
                }
            }
            if hasFTP, let ftp {
                RuleMark(y: .value("FTP", ftp))
                    .foregroundStyle(Palette.muted)
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [5, 4]))
                    .annotation(position: .top, alignment: .trailing) {
                        Text("FTP")
                            .font(.caption2)
                            .foregroundStyle(Palette.muted)
                    }
            }
        }
        .chartXAxisLabel("Time (s)")
        .chartYAxisLabel(hasFTP ? "Power (W)" : "Target (% FTP)")
        .frame(minHeight: 190)
        .accessibilityIdentifier("calendar-workout-profile-chart")
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
    private struct StreamTaskID: Equatable {
        let activityID: Int?
        let sessionID: ObjectIdentifier?
    }

    let activity: JSONValue
    @Environment(SessionGate.self) private var gate
    @State private var streams: ActivityStreams?
    @State private var streamsError: String?
    @State private var isLoading = true

    private var activityID: Int? {
        CalendarData.activityID(activity)
    }

    private var streamTaskID: StreamTaskID {
        StreamTaskID(
            activityID: activityID,
            sessionID: gate.activeSession.map { ObjectIdentifier($0 as AnyObject) }
        )
    }

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
                if activityID != nil, gate.activeSession != nil {
                    if let streams, !StreamSeries.all(in: streams).isEmpty {
                        StreamCharts(streams: streams)
                    } else if isLoading {
                        Panel {
                            HStack(spacing: 10) {
                                ProgressView().tint(Palette.accent)
                                Text("Loading recorded streams…")
                                    .foregroundStyle(Palette.muted)
                            }
                        }
                    } else if let streamsError {
                        Panel {
                            Label(
                                streamsError,
                                systemImage: "waveform.path.ecg.rectangle"
                            )
                            .font(.callout)
                            .foregroundStyle(Palette.muted)
                        }
                    } else if streams != nil {
                        Panel {
                            Label(
                                "No recorded streams for this activity.",
                                systemImage: "waveform.path.ecg.rectangle"
                            )
                            .font(.callout)
                            .foregroundStyle(Palette.muted)
                        }
                    }
                }
            }
            .padding(16)
        }
        .background(Palette.bg)
        .navigationTitle("Ride detail")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: streamTaskID) { await loadStreams() }
    }

    @MainActor private func loadStreams() async {
        streams = nil
        streamsError = nil
        guard let activityID, let session = gate.activeSession else {
            isLoading = false
            return
        }

        isLoading = true
        defer { isLoading = false }
        do {
            streams = try await session.activityStreams(activityID)
            if let streams, StreamSeries.all(in: streams).isEmpty {
                streamsError = "No recorded streams for this activity."
            }
        } catch let failure as CloudSession.Failure {
            if case let .server(.http(status, _, _, _)) = failure, status == 404 {
                streamsError = "No recorded streams for this activity."
            } else {
                streamsError = String(describing: failure)
            }
        } catch {
            streamsError = String(describing: error)
        }
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

    static func profile(_ workout: JSONValue) -> [CalendarWorkoutProfileBlock] {
        CalendarWorkoutProfileDecoder.blocks(from: workout)
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
