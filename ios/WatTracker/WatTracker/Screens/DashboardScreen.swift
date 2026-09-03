import Foundation
import Charts
import Observation
import SwiftUI

/// The first screen a rider sees after opening the app.
///
/// The model deliberately owns the read lifecycle instead of making the view
/// know about credentials, signing or cache files. A cached snapshot is
/// applied before the asynchronous network read, so a returning rider sees
/// their last dashboard immediately.
struct DashboardScreen: View {
    @State private var model: DashboardModel

    init(session: CloudSession? = nil) {
        _model = State(initialValue: DashboardModel(session: session))
    }

    var body: some View {
        ScreenScaffold(
            title: "Dashboard",
            subtitle: "Today's form, fitness and fatigue"
        ) {
            content
        }
        .task { await model.start() }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .starting:
            LoadingDashboard()
        case .unpaired:
            EmptyDashboard(
                symbol: "link.badge.plus",
                title: "Pair your desktop",
                message: "Pair this device to see your training data."
            )
        case .removed:
            EmptyDashboard(
                symbol: "lock.slash",
                title: "Device removed",
                message: "Pair this device again from the desktop to continue."
            )
        case .noData:
            EmptyDashboard(
                symbol: "chart.xyaxis.line",
                title: "No dashboard data yet",
                message: "Your paired desktop has not published training data yet."
            )
        case let .error(message):
            EmptyDashboard(
                symbol: "exclamationmark.triangle",
                title: "Dashboard unavailable",
                message: message
            )
        case let .ready(dashboard):
            DashboardContent(model: model, dashboard: dashboard)
        }
    }
}

@MainActor
@Observable
final class DashboardModel {
    enum State {
        case starting
        case unpaired
        case removed
        case noData
        case error(String)
        case ready(DashboardData)
    }

    private(set) var state: State = .starting
    var selectedWindow: LoadWindow = .sixWeeks

    private let suppliedSession: CloudSession?
    private var started = false

    init(session: CloudSession? = nil) {
        suppliedSession = session
    }

    func start() async {
        guard !started else { return }
        started = true

        let session: CloudSession
        if let suppliedSession {
            session = suppliedSession
        } else {
            do {
                session = try Self.makeSession()
            } catch {
                state = .error("Could not access the device signing key.")
                return
            }
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

        // The session's cache is intentionally checked before load. load may
        // return the same cache after an offline failure, but reading it here
        // is what removes the network from the first-paint path.
        if let cached = session.cached(.dashboard) {
            apply(cached)
        }

        do {
            apply(try await session.load(.dashboard))
        } catch let failure as CloudSession.Failure {
            switch failure {
            case .notPaired:
                state = .unpaired
            case .deviceRemoved:
                state = .removed
            default:
                // A cached, usable dashboard remains visible if the refresh
                // fails for an ordinary network reason. The session normally
                // returns it as .cache; this guard also protects the already-
                // rendered view from a late transport error. Revocation and
                // unpairing are handled above so they always clear the view.
                if case .ready = state { return }
                state = .error(failure.description)
            }
        } catch {
            if case .ready = state { return }
            state = .error(error.localizedDescription)
        }
    }

    private func apply(_ snapshot: CloudSnapshot) {
        let dashboard = DashboardData(snapshot: snapshot)
        state = dashboard.hasAnyData ? .ready(dashboard) : .noData
    }

    private static func makeSession() throws -> CloudSession {
        let signer = try DeviceKeyStore.loadOrCreate()
        let client = CloudClient(
            baseURL: AppConfiguration.apiBaseURL,
            signer: signer
        )
        return CloudSession(
            client: client,
            credentials: KeychainDeviceCredentialStore(),
            cache: FileSnapshotCache()
        )
    }
}

enum LoadWindow: String, CaseIterable, Identifiable, Sendable {
    case sixWeeks = "6 weeks"
    case sixMonths = "6 months"
    case oneYear = "1 year"

    var id: String { rawValue }

    var days: Int {
        switch self {
        case .sixWeeks: return 42
        case .sixMonths: return 183
        case .oneYear: return 365
        }
    }
}

/// The subset of a dashboard snapshot the screen needs to render.
///
/// This stays independent of SwiftUI so extraction and windowing can be
/// checked without a simulator. Unknown and unrelated cloud kinds remain in
/// the cache but do not affect the dashboard.
struct DashboardData: Sendable, Equatable {
    let profile: RiderProfile?
    let training: TrainingState?
    let curve: PowerCurve?
    let loadPoints: [LoadPoint]
    let source: CloudSnapshot.Source
    let asOf: Date

    init(snapshot: CloudSnapshot) {
        var profile: RiderProfile?
        var training: TrainingState?
        var curve: PowerCurve?
        var points: [LoadPoint] = []

        for item in snapshot.items where !item.deleted {
            switch item.payload {
            case let .profile(value): profile = value
            case let .trainingState(value): training = value
            case let .loadPoint(value): points.append(value)
            case let .curve(value): curve = value
            default: break
            }
        }

        self.profile = profile
        self.training = training
        self.curve = curve
        self.loadPoints = points.sorted { lhs, rhs in
            switch (Self.date(lhs.date), Self.date(rhs.date)) {
            case let (left?, right?): return left < right
            case (_?, nil): return true
            case (nil, _?): return false
            case (nil, nil): return (lhs.date ?? "") < (rhs.date ?? "")
            }
        }
        source = snapshot.source
        asOf = snapshot.asOf
    }

    /// A publisher emits the profile/training/curve envelope even when every
    /// value is null or the load series is empty. That envelope is not yet
    /// usable dashboard data and should produce the honest empty state.
    var hasAnyData: Bool {
        if loadPoints.contains(where: { point in
            point.tss != nil || point.ctl != nil || point.atl != nil || point.tsb != nil
        }) {
            return true
        }
        if let profile,
           profile.resolvedFTP != nil || profile.weightKg != nil
            || profile.power?.available == true
            || profile.heartRate?.available == true {
            return true
        }
        if let training,
           training.ftp != nil || training.cp != nil || training.wprime != nil
            || training.decoupling != nil {
            return true
        }
        guard let curve else { return false }
        return !(curve.measured ?? []).isEmpty
            || !(curve.allTime ?? []).isEmpty
            || !(curve.lastRide ?? []).isEmpty
            || !(curve.model ?? []).isEmpty
            || curve.cp != nil
            || curve.wprime != nil
    }

    /// The newest usable load record. A publisher can include a partial or
    /// undated placeholder, which must not displace a dated record when the
    /// training-state object is absent.
    var latestLoadPoint: LoadPoint? {
        loadPoints
            .filter { point in
                Self.date(point.date) != nil
                    && (point.ctl != nil || point.atl != nil || point.tsb != nil)
            }
            .max { lhs, rhs in
                guard let left = Self.date(lhs.date),
                      let right = Self.date(rhs.date) else { return false }
                return left < right
            }
    }

    /// Keep a window relative to the newest point in the snapshot rather than
    /// the phone's current clock. This matters when an old cache is opened
    /// offline: its useful history must not disappear simply because the
    /// calendar moved on while the phone was disconnected.
    func points(for window: LoadWindow) -> [LoadPoint] {
        guard let latest = loadPoints.compactMap({ Self.date($0.date) }).max()
        else { return [] }

        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let cutoff = calendar.date(
            byAdding: .day,
            value: -window.days,
            to: latest
        ) ?? latest
        return loadPoints.filter { point in
            guard let date = Self.date(point.date) else { return false }
            return date >= cutoff && date <= latest
        }
    }

    /// Parse both the date-only load-point wire value and the timestamp shape
    /// used by the other cloud collections. The formatter is local to the
    /// call because DateFormatter is mutable and the screen may map data from
    /// more than one task over its lifetime.
    static func date(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formats = [
            "yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX",
            "yyyy-MM-dd'T'HH:mm:ssXXXXX",
            "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "yyyy-MM-dd'T'HH:mm:ss",
            "yyyy-MM-dd",
        ]
        for format in formats {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.calendar = Calendar(identifier: .gregorian)
            formatter.timeZone = TimeZone(secondsFromGMT: 0)
            formatter.dateFormat = format
            if let date = formatter.date(from: value) { return date }
        }
        return nil
    }

    static func number(_ value: Double?, suffix: String = "") -> String {
        guard let value, value.isFinite else { return "—" }
        let formatted = String(format: "%.1f", value)
        let trimmed = formatted.hasSuffix(".0")
            ? String(formatted.dropLast(2))
            : formatted
        return trimmed + suffix
    }
}

private struct DashboardContent: View {
    @Bindable var model: DashboardModel
    let dashboard: DashboardData

    private var currentLoad: LoadPoint? { dashboard.latestLoadPoint }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if dashboard.source == .cache {
                CacheBanner(asOf: dashboard.asOf)
            }

            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 116), spacing: 8)],
                spacing: 8
            ) {
                MetricTile(
                    title: "FTP",
                    value: DashboardData.number(
                        dashboard.training?.ftp ?? dashboard.profile?.resolvedFTP,
                        suffix: " W"
                    )
                )
                MetricTile(
                    title: "CP",
                    value: DashboardData.number(
                        dashboard.training?.cp ?? dashboard.curve?.cp,
                        suffix: " W"
                    )
                )
                MetricTile(
                    title: "W′",
                    value: DashboardData.number(
                        dashboard.training?.wprime ?? dashboard.curve?.wprime,
                        suffix: " J"
                    )
                )
                MetricTile(
                    title: "Weight",
                    value: DashboardData.number(
                        dashboard.profile?.weightKg,
                        suffix: " kg"
                    )
                )
                MetricTile(
                    title: "CTL",
                    value: DashboardData.number(
                        dashboard.training?.ctl ?? currentLoad?.ctl
                    )
                )
                MetricTile(
                    title: "ATL",
                    value: DashboardData.number(
                        dashboard.training?.atl ?? currentLoad?.atl
                    )
                )
                MetricTile(
                    title: "TSB",
                    value: DashboardData.number(
                        dashboard.training?.tsb ?? currentLoad?.tsb
                    )
                )
            }

            // The first layout is the landscape-first presentation. The
            // second is what ViewThatFits selects for a narrow window, where
            // two compact charts would be cramped. The minimums leave enough
            // room for the iPhone landscape content area after the leading
            // rail has taken its share of the viewport.
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: 12) {
                    LoadChart(
                        points: dashboard.points(for: model.selectedWindow),
                        window: $model.selectedWindow
                    )
                    .frame(minWidth: 360)

                    CurveChart(
                        curve: dashboard.curve,
                        ftp: dashboard.training?.ftp ?? dashboard.profile?.resolvedFTP
                    )
                    .frame(minWidth: 340)
                }

                VStack(spacing: 12) {
                    LoadChart(
                        points: dashboard.points(for: model.selectedWindow),
                        window: $model.selectedWindow
                    )
                    CurveChart(
                        curve: dashboard.curve,
                        ftp: dashboard.training?.ftp ?? dashboard.profile?.resolvedFTP
                    )
                }
            }
        }
    }
}

private struct MetricTile: View {
    let title: String
    let value: String

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
                Text(value)
                    .font(.title3.bold())
                    .monospacedDigit()
                    .foregroundStyle(Palette.textBright)
                    .minimumScaleFactor(0.7)
                    .lineLimit(1)
            }
            .frame(minHeight: 46, alignment: .leading)
        }
    }
}

private struct CacheBanner: View {
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
            "Showing cached dashboard data. Last synced "
                + asOf.formatted(date: .abbreviated, time: .shortened)
        )
    }
}

private struct LoadingDashboard: View {
    var body: some View {
        Panel {
            HStack(spacing: 10) {
                ProgressView()
                    .tint(Palette.accent)
                Text("Syncing dashboard…")
                    .font(.callout)
                    .foregroundStyle(Palette.muted)
            }
        }
    }
}

private struct LoadSeriesPoint: Identifiable {
    enum Metric: String, CaseIterable {
        case ctl = "CTL"
        case atl = "ATL"
        case tsb = "TSB"

        var color: Color {
            switch self {
            case .ctl: return Color(hex: 0x1baf7a)
            case .atl: return Color(hex: 0xc98500)
            case .tsb: return Color(hex: 0x3987e5)
            }
        }
    }

    let id: String
    let date: Date
    let metric: Metric
    let value: Double
}

private struct LoadChart: View {
    let points: [LoadPoint]
    @Binding var window: LoadWindow

    private var series: [LoadSeriesPoint] {
        points.flatMap { point -> [LoadSeriesPoint] in
            guard let date = DashboardData.date(point.date) else { return [] }
            return [
                point.ctl.map {
                    LoadSeriesPoint(
                        id: "\(date.timeIntervalSince1970)-ctl",
                        date: date,
                        metric: .ctl,
                        value: $0
                    )
                },
                point.atl.map {
                    LoadSeriesPoint(
                        id: "\(date.timeIntervalSince1970)-atl",
                        date: date,
                        metric: .atl,
                        value: $0
                    )
                },
                point.tsb.map {
                    LoadSeriesPoint(
                        id: "\(date.timeIntervalSince1970)-tsb",
                        date: date,
                        metric: .tsb,
                        value: $0
                    )
                },
            ].compactMap { $0 }
        }
    }

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .center, spacing: 8) {
                    Text("Training load")
                        .font(.headline)
                        .foregroundStyle(Palette.textBright)
                    Spacer(minLength: 4)
                    Picker("Window", selection: $window) {
                        ForEach(LoadWindow.allCases) { value in
                            Text(value.rawValue).tag(value)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 360)
                }

                if series.isEmpty {
                    EmptyChart(message: "No load history yet")
                } else {
                    Chart(series) { point in
                        LineMark(
                            x: .value("Date", point.date),
                            y: .value("Load", point.value)
                        )
                        .foregroundStyle(by: .value("Series", point.metric.rawValue))
                        .interpolationMethod(.linear)
                    }
                    .chartForegroundStyleScale(
                        domain: LoadSeriesPoint.Metric.allCases.map(\.rawValue),
                        range: LoadSeriesPoint.Metric.allCases.map(\.color)
                    )
                    .chartLegend(position: .bottom, alignment: .leading)
                    .chartYAxisLabel("Load")
                    .frame(minHeight: 210)
                    .accessibilityIdentifier("dashboard-load-chart")
                }
            }
        }
    }
}

private struct CurveSeriesPoint: Identifiable {
    enum Series: String, CaseIterable {
        case measured = "Last 90 days MMP"
        case allTime = "All-time MMP"
        case lastRide = "Last ride MMP"
        case model = "CP/W′ model"

        var color: Color {
            switch self {
            case .measured: return Color(hex: 0xc98500)
            case .allTime: return Color(hex: 0x3987e5)
            case .lastRide: return Color(hex: 0xe05a5a)
            case .model: return Color(hex: 0x1baf7a)
            }
        }
    }

    let id: String
    let duration: Double
    let power: Double
    let series: Series
}

private struct CurveChart: View {
    let curve: PowerCurve?
    let ftp: Double?

    private var points: [CurveSeriesPoint] {
        guard let curve else { return [] }
        let values: [(CurveSeriesPoint.Series, [CurvePoint])] = [
            (.measured, curve.measured ?? []),
            (.allTime, curve.allTime ?? []),
            (.lastRide, curve.lastRide ?? []),
            (.model, curve.model ?? []),
        ]
        return values.flatMap { series, curvePoints in
            curvePoints.compactMap { point in
                guard let duration = point.t, duration > 0,
                      let power = point.power, power.isFinite else { return nil }
                return CurveSeriesPoint(
                    id: "\(series.rawValue)-\(duration)",
                    duration: duration,
                    power: power,
                    series: series
                )
            }
        }
    }

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 10) {
                Text("Power-duration curve")
                    .font(.headline)
                    .foregroundStyle(Palette.textBright)

                if points.isEmpty {
                    EmptyChart(message: "No power data yet")
                } else {
                    Chart {
                        ForEach(points) { point in
                            LineMark(
                                x: .value("Duration", point.duration),
                                y: .value("Power", point.power)
                            )
                            .foregroundStyle(
                                by: .value("Series", point.series.rawValue)
                            )
                            .interpolationMethod(.linear)
                        }

                        if let ftp, ftp.isFinite {
                            RuleMark(y: .value("FTP", ftp))
                                .foregroundStyle(Palette.muted)
                                .lineStyle(
                                    StrokeStyle(lineWidth: 1, dash: [6, 4])
                                )
                                .annotation(
                                    position: .top,
                                    alignment: .trailing
                                ) {
                                    Text("FTP")
                                        .font(.caption2)
                                        .foregroundStyle(Palette.muted)
                                }
                        }
                    }
                    .chartForegroundStyleScale(
                        domain: CurveSeriesPoint.Series.allCases.map(\.rawValue),
                        range: CurveSeriesPoint.Series.allCases.map(\.color)
                    )
                    .chartLegend(position: .bottom, alignment: .leading)
                    .chartXAxisLabel("Duration")
                    .chartYAxisLabel("Power (W)")
                    .chartXScale(type: .log)
                    .frame(minHeight: 240)
                    .accessibilityIdentifier("dashboard-power-curve-chart")
                }
            }
        }
    }
}

private struct EmptyChart: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.callout)
            .foregroundStyle(Palette.muted)
            .frame(maxWidth: .infinity, minHeight: 150, alignment: .center)
    }
}

private struct EmptyDashboard: View {
    let symbol: String
    let title: String
    let message: String

    var body: some View {
        Panel {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: symbol)
                    .font(.title2)
                    .foregroundStyle(Palette.accent)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 6) {
                    Text(title)
                        .font(.headline)
                        .foregroundStyle(Palette.textBright)
                    Text(message)
                        .font(.callout)
                        .foregroundStyle(Palette.muted)
                }
            }
        }
    }
}

private extension Color {
    init(hex: UInt32) { self = Palette.hex(hex) }
}
