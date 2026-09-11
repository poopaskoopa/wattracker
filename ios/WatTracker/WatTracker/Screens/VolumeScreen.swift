import Charts
import Foundation
import Observation
import SwiftUI

struct VolumeScreen: View {
    @Environment(\.cloudSession) private var session
    @State private var model = VolumeScreenModel()

    var body: some View {
        ScreenScaffold(
            title: "Volume",
            subtitle: "Weekly training totals"
        ) {
            content
        }
        .task { await model.start(session: session) }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .starting:
            VolumeStatusPanel(
                symbol: "arrow.triangle.2.circlepath",
                title: "Syncing volume…",
                showsProgress: true
            )
        case .unpaired:
            VolumeStatusPanel(
                symbol: "link.badge.plus",
                title: "Pair your desktop",
                message: "Pair this device to see your training volume."
            )
        case .removed:
            VolumeStatusPanel(
                symbol: "lock.slash",
                title: "Device removed",
                message: "Pair this device again from the desktop to continue."
            )
        case .noData:
            VolumeStatusPanel(
                symbol: "chart.bar",
                title: "No volume data yet",
                message: "Your paired desktop has not published any weekly totals yet."
            )
        case let .error(message):
            VolumeStatusPanel(
                symbol: "exclamationmark.triangle",
                title: "Volume unavailable",
                message: message
            )
        case let .ready(data):
            VolumeContent(model: model, data: data)
        }
    }
}

@MainActor
@Observable
final class VolumeScreenModel {
    enum State {
        case starting
        case unpaired
        case removed
        case noData
        case error(String)
        case ready(VolumeData)
    }

    private(set) var state: State = .starting
    var selectedMetric: VolumeMetric = .hours
    var selectedRange: VolumeRange = .last12

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

        if let cached = session.cached(.volume) {
            apply(cached)
        }

        do {
            apply(try await session.load(.volume))
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

    private func apply(_ snapshot: CloudSnapshot) {
        let data = VolumeData(snapshot: snapshot)
        state = data.weeks.isEmpty ? .noData : .ready(data)
    }
}

private struct VolumeContent: View {
    @Bindable var model: VolumeScreenModel
    let data: VolumeData

    private var weeks: [VolumeWeek] {
        data.weeks(for: model.selectedRange)
    }

    private var summary: VolumeSummary {
        data.summary(for: model.selectedRange)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if data.source == .cache {
                VolumeCacheBanner(asOf: data.asOf)
            }

            Panel {
                VStack(alignment: .leading, spacing: 12) {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 12) {
                            VolumeMetricPicker(selection: $model.selectedMetric)
                            Spacer(minLength: 8)
                            VolumeRangePicker(selection: $model.selectedRange)
                        }

                        VStack(alignment: .leading, spacing: 10) {
                            VolumeMetricPicker(selection: $model.selectedMetric)
                            VolumeRangePicker(selection: $model.selectedRange)
                        }
                    }

                    VolumeChart(
                        weeks: weeks,
                        metric: model.selectedMetric
                    )
                }
            }

            VolumeSummaryGrid(
                summary: summary,
                range: model.selectedRange,
                selectedMetric: model.selectedMetric,
                change: data.change(
                    for: model.selectedMetric,
                    range: model.selectedRange
                )
            )

            VolumePeriodSummaryRow(
                data: data,
                metric: model.selectedMetric
            )
        }
    }
}

private struct VolumeMetricPicker: View {
    @Binding var selection: VolumeMetric

    var body: some View {
        HStack(spacing: 8) {
            Text("Metric")
                .font(.caption)
                .foregroundStyle(Palette.muted)
            Picker("Metric", selection: $selection) {
                ForEach(VolumeMetric.allCases) { metric in
                    Text(metric.title).tag(metric)
                }
            }
            .pickerStyle(.menu)
            .tint(Palette.accent)
            .accessibilityIdentifier("volume-metric-picker")
        }
    }
}

private struct VolumeRangePicker: View {
    @Binding var selection: VolumeRange

    var body: some View {
        Picker("Range", selection: $selection) {
            ForEach(VolumeRange.allCases) { range in
                Text(range.shortTitle).tag(range)
            }
        }
        .pickerStyle(.segmented)
        .frame(maxWidth: 340)
        .accessibilityIdentifier("volume-range-picker")
    }
}

private struct VolumeChartPoint: Identifiable {
    let weekStart: Date
    let value: Double

    var id: Date { weekStart }
}

private struct VolumeChart: View {
    let weeks: [VolumeWeek]
    let metric: VolumeMetric

    private var points: [VolumeChartPoint] {
        weeks.compactMap { week in
            guard let date = VolumeFormat.date(week.weekStart) else { return nil }
            return VolumeChartPoint(
                weekStart: date,
                value: metric.value(from: week)
            )
        }
    }

    private var average: Double? {
        guard !points.isEmpty else { return nil }
        return points.reduce(0) { $0 + $1.value } / Double(points.count)
    }

    var body: some View {
        if points.isEmpty {
            VStack(spacing: 8) {
                Image(systemName: "chart.bar")
                    .font(.title2)
                    .foregroundStyle(Palette.muted)
                Text("No weeks in this range")
                    .font(.callout)
                    .foregroundStyle(Palette.muted)
            }
            .frame(maxWidth: .infinity, minHeight: 220)
        } else {
            Chart {
                ForEach(points) { point in
                    BarMark(
                        x: .value("Week", point.weekStart, unit: .weekOfYear),
                        y: .value(metric.title, point.value)
                    )
                    .foregroundStyle(Palette.hex(0x3987e5).gradient)
                    .cornerRadius(3)
                }

                if let average {
                    RuleMark(y: .value("Weekly average", average))
                        .foregroundStyle(Palette.accent)
                        .lineStyle(StrokeStyle(lineWidth: 1, dash: [5, 4]))
                        .annotation(position: .top, alignment: .trailing) {
                            Text("avg \(VolumeFormat.value(average, metric: metric))")
                                .font(.caption2.monospacedDigit())
                                .foregroundStyle(Palette.accent)
                        }
                }
            }
            .chartYAxisLabel(metric.unit)
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 6)) { _ in
                    AxisGridLine()
                        .foregroundStyle(Palette.surfaceBorder)
                    AxisTick()
                    AxisValueLabel(format: .dateTime.month(.abbreviated).day())
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading) { _ in
                    AxisGridLine()
                        .foregroundStyle(Palette.surfaceBorder)
                    AxisValueLabel()
                }
            }
            .frame(minHeight: 230)
            .accessibilityIdentifier("volume-chart")
        }
    }
}

private struct VolumeSummaryGrid: View {
    let summary: VolumeSummary
    let range: VolumeRange
    let selectedMetric: VolumeMetric
    let change: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(range.title) totals")
                    .font(.headline)
                    .foregroundStyle(Palette.textBright)
                Spacer()
                if let change {
                    Label(
                        VolumeFormat.percent(change),
                        systemImage: change >= 0
                            ? "arrow.up.right"
                            : "arrow.down.right"
                    )
                    .font(.caption.bold())
                    .foregroundStyle(change >= 0 ? Palette.ok : Palette.alert)
                    .accessibilityLabel(
                        "\(selectedMetric.title) changed "
                            + VolumeFormat.percent(change)
                            + " from the previous period"
                    )
                }
            }
            .padding(.horizontal, 4)

            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 124), spacing: 8)],
                spacing: 8
            ) {
                ForEach(VolumeMetric.allCases) { metric in
                    VolumeSummaryTile(
                        title: metric.title,
                        value: VolumeFormat.value(
                            summary.value(for: metric),
                            metric: metric
                        ),
                        selected: metric == selectedMetric
                    )
                }
            }
        }
    }
}

private struct VolumePeriodSummaryRow: View {
    let data: VolumeData
    let metric: VolumeMetric

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Period summary · \(metric.title)")
                .font(.headline)
                .foregroundStyle(Palette.textBright)
                .padding(.horizontal, 4)

            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 124), spacing: 8)],
                spacing: 8
            ) {
                ForEach(VolumeRange.allCases) { range in
                    let value = data.summary(for: range).value(for: metric)
                    Panel {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(range.title)
                                .font(.caption)
                                .foregroundStyle(Palette.muted)
                            Text(VolumeFormat.value(value, metric: metric))
                                .font(.title3.bold())
                                .monospacedDigit()
                                .foregroundStyle(Palette.textBright)
                                .lineLimit(1)
                                .minimumScaleFactor(0.65)
                        }
                        .frame(minHeight: 46, alignment: .leading)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(
                        "\(range.title): \(VolumeFormat.value(value, metric: metric))"
                    )
                }
            }
        }
    }
}

private struct VolumeSummaryTile: View {
    let title: String
    let value: String
    let selected: Bool

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(selected ? Palette.accent : Palette.muted)
                Text(value)
                    .font(.title3.bold())
                    .monospacedDigit()
                    .foregroundStyle(Palette.textBright)
                    .lineLimit(1)
                    .minimumScaleFactor(0.65)
            }
            .frame(minHeight: 46, alignment: .leading)
        }
        .overlay {
            if selected {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(Palette.accent.opacity(0.7), lineWidth: 1)
            }
        }
    }
}

private struct VolumeCacheBanner: View {
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
    }
}

private struct VolumeStatusPanel: View {
    let symbol: String
    let title: String
    var message: String?
    var showsProgress = false

    var body: some View {
        Panel {
            HStack(spacing: 12) {
                if showsProgress {
                    ProgressView()
                        .tint(Palette.accent)
                } else {
                    Image(systemName: symbol)
                        .font(.title2)
                        .foregroundStyle(Palette.accent)
                        .frame(width: 30)
                }
                VStack(alignment: .leading, spacing: 3) {
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

private enum VolumeFormat {
    static func date(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.date(from: value)
    }

    static func value(_ value: Double, metric: VolumeMetric) -> String {
        let decimals = metric == .hours || metric == .distanceKm ? 1 : 0
        let format = decimals == 0 ? "%.0f" : "%.1f"
        return String(format: format, locale: .current, value) + " " + metric.unit
    }

    static func percent(_ value: Double) -> String {
        String(format: "%+.0f%%", locale: .current, value * 100)
    }
}
