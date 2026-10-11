import Charts
import SwiftUI

struct ActivitiesScreen: View {
    /// The one session, owned by `SessionGate` and injected by `AppGate`.
    @Environment(\.readSession) private var session
    @Environment(SessionGate.self) private var gate
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @State private var model = ActivitiesModel()

    var body: some View {
        Group {
            if usesTwoPaneLayout {
                HStack(spacing: 0) {
                    activityList(selectionEnabled: true)
                        .frame(minWidth: 330, idealWidth: 390, maxWidth: 440)
                    Divider().overlay(Palette.surfaceBorder)
                    detailPane
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .id(model.selectedID)
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
        .task(id: gate.backend) { await model.refresh(session: gate.activeSession) }
        .onChange(of: rides.map(\.id)) { _, ids in
            if model.selectedID == nil || !ids.contains(model.selectedID ?? "") {
                model.selectedID = ids.first
            }
        }
    }

    private var usesTwoPaneLayout: Bool {
        horizontalSizeClass == .regular && UIDevice.current.userInterfaceIdiom == .pad
    }

    private var rides: [RideSummary] { model.rides }

    @ViewBuilder private var detailPane: some View {
        if let selectedID = model.selectedID,
           let ride = rides.first(where: { $0.id == selectedID }),
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
                switch model.availability {
                case .unpaired:
                    ContentUnavailableView(
                        "Pair your desktop", systemImage: "link.badge.plus",
                        description: Text("Pair this device to see your rides.")
                    )
                    .listRowBackground(Palette.bg)
                case .removed:
                    ContentUnavailableView(
                        "Device removed", systemImage: "lock.slash",
                        description: Text(
                            "Pair this device again from the desktop to continue."
                        )
                    )
                    .listRowBackground(Palette.bg)
                case .available:
                    if let message = model.errorMessage, rides.isEmpty {
                        ContentUnavailableView(
                            "Activities unavailable", systemImage: "wifi.exclamationmark",
                            description: Text(message)
                        )
                        .listRowBackground(Palette.bg)
                    } else if rides.isEmpty, !model.isLoading {
                        ContentUnavailableView(
                            "No activities", systemImage: "bicycle",
                            description: Text("Recorded rides appear after the desktop syncs.")
                        )
                        .listRowBackground(Palette.bg)
                    } else {
                        ForEach(rides) { ride in
                            if selectionEnabled {
                                Button { model.selectedID = ride.id } label: {
                                    ActivityRow(ride: ride)
                                }
                                .buttonStyle(.plain)
                                .listRowBackground(
                                    model.selectedID == ride.id ? Palette.surface2 : Palette.bg
                                )
                            } else {
                                NavigationLink(value: ride.id) { ActivityRow(ride: ride) }
                                    .listRowBackground(Palette.bg)
                            }
                        }
                    }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .overlay {
                if model.isLoading, model.snapshot == nil { ProgressView().tint(Palette.accent) }
            }
            .refreshable { await model.refresh(session: session, userInitiated: true) }
        }
        .background(Palette.bg)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Activities").font(.title2.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                Spacer()
                if model.snapshot?.source == .cache {
                    Label("Offline", systemImage: "icloud.slash")
                        .font(.caption.weight(.medium)).foregroundStyle(Palette.muted)
                }
            }
            Text(model.snapshot.map { "Newest first · updated \(RideFormatting.relative($0.asOf))" }
                ?? "Every recorded ride")
                .font(.subheadline).foregroundStyle(Palette.muted)
            if model.wake.isShowing {
                Label("Waking up the cloud…", systemImage: "cloud")
                    .font(.caption).foregroundStyle(Palette.muted)
            }
            if let errorMessage = model.errorMessage, !rides.isEmpty {
                Text(errorMessage).font(.caption).foregroundStyle(Palette.alert).lineLimit(1)
            }
        }
        .padding(16)
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
    let session: any ReadSession
    @State private var detail: ActivityDetail?
    @State private var streams: ActivityStreams?
    @State private var detailError: String?
    @State private var streamsError: String?
    @State private var isLoading = true
    @State private var wake = CloudWakeNotice()

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(RideFormatting.date(ride.summary.startTime))
                        .font(.title2.weight(.semibold)).foregroundStyle(Palette.textBright)
                    Text("Activity detail").font(.subheadline).foregroundStyle(Palette.muted)
                    if wake.isShowing {
                        Label("Waking up the cloud…", systemImage: "cloud")
                            .font(.caption).foregroundStyle(Palette.muted)
                    }
                }
                summaryPanel
                if let detail { ZoneSection(zones: detail.zones) }
                if let streams, !StreamSeries.all(in: streams).isEmpty {
                    StreamCharts(streams: streams, durationSeconds: ride.summary.durationS)
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
        async let detailResult = wake.during(session) {
            try await session.activityDetail(ride.activityID)
        }
        async let streamResult = wake.during(session) {
            try await session.activityStreams(ride.activityID)
        }
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

struct StreamCharts: View {
    let streams: ActivityStreams
    var durationSeconds: Double?
    @State private var hidden: Set<String> = []

    var body: some View {
        let series = CombinedChartSeries.build(from: streams, durationSeconds: durationSeconds)
        let visible = series.filter { !hidden.contains($0.id) }
        let all = StreamSeries.all(in: streams)
        let powerAxis = visible.contains { $0.id == "power" }
            ? CombinedChartSeries.axis(.power, in: all) : nil
        let sharedAxis = visible.contains { $0.id == "heart-rate" || $0.id == "cadence" }
            ? CombinedChartSeries.axis(.shared, in: all) : nil
        let extent = CombinedChartSeries.extent(series)
        let xTicks = ChartTicks.minutes(upTo: extent)
        Panel {
            VStack(alignment: .leading, spacing: 8) {
                Text("Ride graphs").font(.headline).foregroundStyle(Palette.textBright)
                HStack {
                    Text(powerAxis?.title ?? "").foregroundStyle(Palette.accent)
                    Spacer()
                    Text(sharedAxis?.title ?? "").foregroundStyle(Palette.muted)
                }
                .font(.caption2)
                Chart {
                    ForEach(visible) { s in
                        ForEach(s.points) { p in
                            if s.isBackground {
                                AreaMark(x: .value("Minutes", p.minutes),
                                         y: .value(s.title, p.scaled),
                                         series: .value("Series", s.id))
                                    .foregroundStyle(s.color.opacity(CombinedChartSeries.altitudeFillOpacity))
                                    .interpolationMethod(.linear)
                                LineMark(x: .value("Minutes", p.minutes),
                                         y: .value(s.title, p.scaled),
                                         series: .value("Series", s.id))
                                    .foregroundStyle(s.color.opacity(CombinedChartSeries.altitudeLineOpacity))
                                    .lineStyle(StrokeStyle(lineWidth: 1))
                                    .interpolationMethod(.linear)
                            } else {
                                LineMark(x: .value("Minutes", p.minutes),
                                         y: .value(s.title, p.scaled),
                                         series: .value("Series", s.id))
                                    .foregroundStyle(s.color)
                                    .lineStyle(StrokeStyle(lineWidth: 1.5))
                                    .interpolationMethod(.linear)
                            }
                        }
                    }
                }
                .chartForegroundStyleScale(domain: series.map(\.id), range: series.map(\.color))
                .chartLegend(.hidden)
                .chartYScale(domain: 0...1)
                .chartXScale(domain: 0...max(extent, 1.0 / 60))
                .chartYAxis {
                    if let powerAxis {
                        AxisMarks(position: .leading, values: powerAxis.scaledTicks) { value in
                            AxisGridLine().foregroundStyle(Palette.surfaceBorder)
                            AxisValueLabel {
                                if let p = value.as(Double.self) {
                                    Text(Self.tickLabel(powerAxis.value(atScaled: p)))
                                }
                            }
                            .foregroundStyle(Palette.muted)
                        }
                    }
                    if let sharedAxis {
                        AxisMarks(position: .trailing, values: sharedAxis.scaledTicks) { value in
                            if powerAxis == nil { AxisGridLine().foregroundStyle(Palette.surfaceBorder) }
                            AxisValueLabel {
                                if let p = value.as(Double.self) {
                                    Text(Self.tickLabel(sharedAxis.value(atScaled: p)))
                                }
                            }
                            .foregroundStyle(Palette.muted)
                        }
                    }
                }
                .chartXAxis {
                    AxisMarks(values: xTicks) { value in
                        AxisGridLine().foregroundStyle(Palette.surfaceBorder)
                        AxisValueLabel {
                            if let m = value.as(Double.self) { Text(Self.tickLabel(m)) }
                        }
                        .foregroundStyle(Palette.muted)
                    }
                }
                .frame(height: 220)
                .accessibilityLabel("Ride graphs chart")
                Text("Time (min). Altitude is a faint background without an axis.")
                    .font(.caption2).foregroundStyle(Palette.muted)
                legend(series)
            }
        }
    }

    private static func range(_ s: CombinedChartSeries) -> String {
        let values = s.points.map(\.value)
        guard let lo = values.min(), let hi = values.max() else { return "" }
        let low = s.isBackground ? lo : 0
        return "\(Int(low.rounded()))-\(Int(hi.rounded()))"
    }

    static func tickLabel(_ value: Double) -> String {
        let rounded = (value * 10).rounded() / 10
        return rounded == rounded.rounded() ? String(Int(rounded)) : String(format: "%.1f", rounded)
    }

    private func legend(_ series: [CombinedChartSeries]) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), alignment: .leading)],
                  alignment: .leading, spacing: 6) {
            ForEach(series) { s in
                let off = hidden.contains(s.id)
                Button {
                    if off { hidden.remove(s.id) } else { hidden.insert(s.id) }
                } label: {
                    HStack(spacing: 6) {
                        Circle().fill(s.color).frame(width: 9, height: 9)
                        Text("\(s.title) \(Self.range(s))")
                            .font(.caption).foregroundStyle(Palette.text)
                    }
                    .opacity(off ? 0.4 : 1)
                }
                .buttonStyle(.plain)
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
                            Text(row.durationText)
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
