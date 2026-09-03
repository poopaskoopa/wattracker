import Charts
import SwiftUI

struct ActivitiesScreen: View {
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @State private var session: CloudSession?
    @State private var snapshot: CloudSnapshot?
    @State private var selectedID: String?
    @State private var errorMessage: String?
    @State private var startupError: String?
    @State private var isLoading = false

    init(session injectedSession: CloudSession? = nil) {
        if let injectedSession {
            _session = State(initialValue: injectedSession)
            _snapshot = State(initialValue: injectedSession.cached(.activities))
            return
        }
        do {
            let signer = try DeviceKeyStore.loadOrCreate()
            let resolved = CloudSession(
                client: CloudClient(baseURL: AppConfiguration.apiBaseURL, signer: signer),
                credentials: KeychainDeviceCredentialStore(), cache: FileSnapshotCache()
            )
            _session = State(initialValue: resolved)
            _snapshot = State(initialValue: resolved.cached(.activities))
        } catch {
            _session = State(initialValue: nil)
            _snapshot = State(initialValue: nil)
            _startupError = State(initialValue: String(describing: error))
        }
    }

    var body: some View {
        Group {
            if usesTwoPaneLayout {
                HStack(spacing: 0) {
                    activityList(selectionEnabled: true)
                        .frame(minWidth: 330, idealWidth: 390, maxWidth: 440)
                    Divider().overlay(Palette.surfaceBorder)
                    detailPane
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .id(selectedID)
                }
            } else {
                NavigationStack {
                    activityList(selectionEnabled: false)
                        .navigationDestination(for: String.self) { id in
                            if let ride = rides.first(where: { $0.id == id }),
                               let session {
                                ActivityDetailScreen(ride: ride, session: session)
                            }
                        }
                }
            }
        }
        .background(Palette.bg)
        .task { await refresh() }
        .onChange(of: rides.map(\.id)) { _, ids in
            if selectedID == nil || !ids.contains(selectedID ?? "") {
                selectedID = ids.first
            }
        }
    }

    private var usesTwoPaneLayout: Bool {
        horizontalSizeClass == .regular && UIDevice.current.userInterfaceIdiom == .pad
    }

    private var rides: [RideSummary] {
        (snapshot?.items ?? []).compactMap(RideSummary.init(item:)).sorted {
            if $0.startedAt != $1.startedAt { return $0.startedAt > $1.startedAt }
            return $0.id > $1.id
        }
    }

    @ViewBuilder private var detailPane: some View {
        if let selectedID, let ride = rides.first(where: { $0.id == selectedID }),
           let session {
            ActivityDetailScreen(ride: ride, session: session)
        } else {
            ContentUnavailableView(
                "Select an activity", systemImage: "bicycle",
                description: Text("Ride details and recorded streams appear here.")
            )
            .foregroundStyle(Palette.muted)
        }
    }

    private func activityList(selectionEnabled: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            List {
                if let message = startupError ?? errorMessage, rides.isEmpty {
                    ContentUnavailableView(
                        "Activities unavailable", systemImage: "wifi.exclamationmark",
                        description: Text(message)
                    )
                    .listRowBackground(Palette.bg)
                } else if rides.isEmpty, !isLoading {
                    ContentUnavailableView(
                        "No activities", systemImage: "bicycle",
                        description: Text("Recorded rides appear after the desktop syncs.")
                    )
                    .listRowBackground(Palette.bg)
                } else {
                    ForEach(rides) { ride in
                        if selectionEnabled {
                            Button { selectedID = ride.id } label: {
                                ActivityRow(ride: ride)
                            }
                            .buttonStyle(.plain)
                            .listRowBackground(
                                selectedID == ride.id ? Palette.surface2 : Palette.bg
                            )
                        } else {
                            NavigationLink(value: ride.id) { ActivityRow(ride: ride) }
                                .listRowBackground(Palette.bg)
                        }
                    }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .overlay {
                if isLoading, snapshot == nil { ProgressView().tint(Palette.accent) }
            }
            .refreshable { await refresh() }
        }
        .background(Palette.bg)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Activities").font(.title2.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                Spacer()
                if snapshot?.source == .cache {
                    Label("Offline", systemImage: "icloud.slash")
                        .font(.caption.weight(.medium)).foregroundStyle(Palette.muted)
                }
            }
            Text(snapshot.map { "Newest first · updated \(RideFormatting.relative($0.asOf))" }
                ?? "Every recorded ride")
                .font(.subheadline).foregroundStyle(Palette.muted)
            if let errorMessage, !rides.isEmpty {
                Text(errorMessage).font(.caption).foregroundStyle(Palette.alert).lineLimit(1)
            }
        }
        .padding(16)
    }

    @MainActor private func refresh() async {
        guard let session, startupError == nil else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            snapshot = try await session.load(.activities)
            errorMessage = nil
            if selectedID == nil { selectedID = rides.first?.id }
        } catch {
            errorMessage = String(describing: error)
        }
    }
}

private struct ActivityRow: View {
    let ride: RideSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(RideFormatting.date(ride.summary.startTime))
                    .font(.headline).foregroundStyle(Palette.textBright)
                Spacer()
                Text(RideFormatting.duration(ride.summary.durationS))
                    .font(.subheadline.monospacedDigit()).foregroundStyle(Palette.muted)
            }
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 66), alignment: .leading)],
                alignment: .leading, spacing: 7
            ) {
                RideMetric("DIST", RideFormatting.distance(ride.summary.distanceM))
                RideMetric("AVG", RideFormatting.watts(ride.summary.avgPower))
                RideMetric("NP", RideFormatting.watts(ride.summary.np))
                RideMetric("IF", RideFormatting.decimal(ride.summary.intensityFactor, 2))
                RideMetric("TSS", RideFormatting.decimal(ride.summary.tss, 0))
                RideMetric("RPE", RideFormatting.decimal(ride.summary.rpe, 0))
            }
        }
        .padding(.vertical, 7).contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

private struct RideMetric: View {
    let label: String
    let value: String

    init(_ label: String, _ value: String) { self.label = label; self.value = value }

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(.caption2.weight(.semibold)).foregroundStyle(Palette.muted)
            Text(value).font(.callout.monospacedDigit()).foregroundStyle(Palette.text)
        }
    }
}

private struct ActivityDetailScreen: View {
    let ride: RideSummary
    let session: CloudSession
    @State private var detail: ActivityDetail?
    @State private var streams: ActivityStreams?
    @State private var detailError: String?
    @State private var streamsError: String?
    @State private var isLoading = true

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(RideFormatting.date(ride.summary.startTime))
                        .font(.title2.weight(.semibold)).foregroundStyle(Palette.textBright)
                    Text("Activity detail").font(.subheadline).foregroundStyle(Palette.muted)
                }
                summaryPanel
                if let detail { ZoneSection(zones: detail.zones) }
                if let streams, !StreamSeries.all(in: streams).isEmpty {
                    StreamCharts(streams: streams)
                } else if isLoading {
                    Panel {
                        HStack(spacing: 10) {
                            ProgressView().tint(Palette.accent)
                            Text("Loading recorded streams…").foregroundStyle(Palette.muted)
                        }
                    }
                } else if let streamsError {
                    Panel {
                        Label(streamsError, systemImage: "waveform.path.ecg.rectangle")
                            .font(.callout).foregroundStyle(Palette.muted)
                    }
                } else if streams != nil {
                    Panel {
                        Label(
                            "No recorded streams for this activity.",
                            systemImage: "waveform.path.ecg.rectangle"
                        )
                        .font(.callout).foregroundStyle(Palette.muted)
                    }
                }
                if let detailError {
                    Text(detailError).font(.caption).foregroundStyle(Palette.alert)
                }
            }
            .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Palette.bg)
        .navigationTitle("Ride").navigationBarTitleDisplayMode(.inline)
        .task(id: ride.activityID) { await load() }
    }

    private var summaryPanel: some View {
        Panel {
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 92), alignment: .leading)],
                alignment: .leading, spacing: 12
            ) {
                RideMetric("DURATION", RideFormatting.duration(ride.summary.durationS))
                RideMetric("DISTANCE", RideFormatting.distance(ride.summary.distanceM))
                RideMetric("AVG POWER", RideFormatting.watts(ride.summary.avgPower))
                RideMetric("NP", RideFormatting.watts(ride.summary.np))
                RideMetric("IF", RideFormatting.decimal(ride.summary.intensityFactor, 2))
                RideMetric("TSS", RideFormatting.decimal(ride.summary.tss, 0))
                RideMetric("RPE", RideFormatting.decimal(ride.summary.rpe, 0))
                if let avgHr = ride.summary.avgHr {
                    RideMetric("AVG HR", "\(Int(avgHr.rounded())) bpm")
                }
            }
        }
    }

    @MainActor private func load() async {
        detail = nil
        streams = nil
        isLoading = true
        detailError = nil
        streamsError = nil
        async let detailResult = session.activityDetail(ride.activityID)
        async let streamResult = session.activityStreams(ride.activityID)
        do { detail = try await detailResult } catch {
            detailError = String(describing: error)
        }
        do {
            streams = try await streamResult
            if let streams, StreamSeries.all(in: streams).isEmpty {
                streamsError = "No recorded streams for this activity."
            }
        } catch let failure as CloudSession.Failure {
            if case let .server(.http(status, _, _, _)) = failure, status == 404 {
                streamsError = "No recorded streams for this activity."
            } else { streamsError = String(describing: failure) }
        } catch { streamsError = String(describing: error) }
        isLoading = false
    }
}

private struct StreamCharts: View {
    let streams: ActivityStreams
    var body: some View {
        ForEach(StreamSeries.all(in: streams)) { series in
            Panel {
                VStack(alignment: .leading, spacing: 8) {
                    Text(series.title).font(.headline).foregroundStyle(Palette.textBright)
                    Chart(series.points) { point in
                        LineMark(x: .value("Seconds", point.time),
                                 y: .value(series.title, point.value))
                            .foregroundStyle(series.color).interpolationMethod(.linear)
                    }
                    .chartXAxis(.hidden)
                    .chartYAxis {
                        AxisMarks(position: .leading) {
                            AxisGridLine().foregroundStyle(Palette.surfaceBorder)
                            AxisValueLabel().foregroundStyle(Palette.muted)
                        }
                    }
                    .frame(height: 145).accessibilityLabel("\(series.title) chart")
                }
            }
        }
    }
}

private struct ZoneSection: View {
    let zones: JSONValue?
    var body: some View {
        ForEach(ZoneGroup.extract(from: zones)) { group in
            Panel {
                VStack(alignment: .leading, spacing: 8) {
                    Text(group.title).font(.headline).foregroundStyle(Palette.textBright)
                    ForEach(group.rows) { row in
                        HStack {
                            Text(row.label).foregroundStyle(Palette.text)
                            Spacer()
                            Text(RideFormatting.duration(row.seconds))
                                .monospacedDigit().foregroundStyle(Palette.muted)
                            Text("\(Int(row.percent.rounded()))%")
                                .monospacedDigit().frame(width: 42, alignment: .trailing)
                                .foregroundStyle(Palette.text)
                        }
                        .font(.callout)
                    }
                }
            }
        }
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

private struct StreamPoint: Identifiable { let id: Int; let time: Double; let value: Double }

private struct StreamSeries: Identifiable {
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

private struct ZoneGroup: Identifiable {
    let id: String
    let title: String
    let rows: [ZoneRow]

    static func extract(from value: JSONValue?) -> [ZoneGroup] {
        [("power", "Power zones"), ("heart_rate", "Heart-rate zones")].compactMap {
            key, title in
            guard case let .array(values)? = value?[key]?["zones"] else { return nil }
            let rows = values.enumerated().compactMap { index, value -> ZoneRow? in
                guard let seconds = value["seconds"]?.doubleValue, seconds > 0 else {
                    return nil
                }
                return ZoneRow(
                    id: "\(key)-\(index)",
                    label: value["label"]?.stringValue ?? "Z\(index + 1)",
                    seconds: seconds, percent: value["percent"]?.doubleValue ?? 0
                )
            }
            return rows.isEmpty ? nil : ZoneGroup(id: key, title: title, rows: rows)
        }
    }
}

private struct ZoneRow: Identifiable {
    let id: String; let label: String; let seconds: Double; let percent: Double
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
        let total = Int(seconds.rounded()), hours = total / 3_600
        let minutes = (total % 3_600) / 60, remainder = total % 60
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, remainder)
            : String(format: "%d:%02d", minutes, remainder)
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
