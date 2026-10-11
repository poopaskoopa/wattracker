import XCTest

final class RideDisplayTests: XCTestCase {
    func testRideSummaryPreservesMissingHeartRateAndPower() {
        let summary = ActivitySummary(id: 7, startTime: nil, durationS: 3661,
                                      distanceM: 1234.5, avgPower: nil, avgHr: nil,
                                      np: nil, intensityFactor: nil, tss: nil, rpe: nil)
        let item = CloudItem(id: "activity-7", kind: .activity, revision: 1,
                             deleted: false, payload: .activity(summary))
        let ride = RideSummary(item: item)
        XCTAssertEqual(ride?.activityID, 7)
        XCTAssertNil(ride?.summary.avgHr)
        XCTAssertNil(ride?.summary.avgPower)
        XCTAssertEqual(ride?.startedAt, "")
    }

    func testFormattingMatchesDesktopDurationRoundingAndPadding() {
        // Desktop formats durations in wattracker/races.py:627-637.
        XCTAssertEqual(RideFormatting.duration(309), "05:09")
        XCTAssertEqual(RideFormatting.duration(3599.5), "1:00:00")
        XCTAssertEqual(RideFormatting.duration(3600.5), "1:00:00")
        XCTAssertEqual(RideFormatting.duration(3601), "1:00:01")
    }

    func testFormattingHandlesDistanceAndMissingValues() {
        // The desktop detail card divides meters by 1,000 and formats one
        // decimal place in wattracker/web/templates/activity_detail.html:13.
        XCTAssertEqual(RideFormatting.distance(12345), "12.3 km")
        XCTAssertEqual(RideFormatting.distance(nil), "—")
        XCTAssertEqual(RideFormatting.watts(0), "0 W")
        XCTAssertEqual(RideFormatting.watts(nil), "—")
    }

    func testZoneDurationUsesZoneFormatterNotRideCardPadding() {
        let row = ZoneRow(id: "power-1", label: "Z1", seconds: 309, percent: 100)
        // Desktop zone rows use wattracker/analysis/zones.py:55-59.
        XCTAssertEqual(ZoneFormatting.duration(row.seconds), "5:09")
        XCTAssertEqual(row.durationText, "5:09")
        XCTAssertEqual(RideFormatting.duration(row.seconds), "05:09")

        // Desktop rounds half to even: `int(round(seconds))` in Python 3.
        XCTAssertEqual(ZoneFormatting.duration(2.5), "0:02")
        XCTAssertEqual(ZoneFormatting.duration(0.5), "0:00")
        XCTAssertEqual(ZoneFormatting.duration(59.5), "1:00")
        XCTAssertEqual(ZoneFormatting.duration(3725), "1:02:05")
    }

    func testZoneDurationPrefersPayloadStringAndFallsBackWhenMissing() throws {
        let zones: JSONValue = .object([
            "power": .object(["zones": .array([
                .object([
                    "seconds": .number(2.5),
                    "duration": .string("0:03"),
                    "percent": .number(50)
                ]),
                .object(["seconds": .number(2.5), "percent": .number(50)])
            ])])
        ])

        let rows = try XCTUnwrap(ZoneGroup.extract(from: zones).first?.rows)
        // The server's duration comes from the unrounded sample; use it when
        // present, but keep the local half-to-even formatter as the fallback.
        XCTAssertEqual(rows.map(\.durationText), ["0:03", "0:02"])
        XCTAssertEqual(ZoneFormatting.duration(rows[0].seconds), "0:02")
    }

    func testStreamsDropGapsAndUseTimeChannelWhenPresent() {
        let channels = ActivityStreams.Channels(
            time: nil, power: [0, nil, .infinity],
            heartrate: [nil, nil], cadence: [nil, nil], altitude: nil
        )
        let series = StreamSeries.all(in: ActivityStreams(streams: channels))
        XCTAssertEqual(series.count, 1)
        XCTAssertEqual(series[0].id, "power")
        XCTAssertEqual(series[0].points.map(\.time), [0])
        XCTAssertEqual(series[0].points.map(\.value), [0])
        // Equivalent to the desktop `have` map in wattracker/analysis/pipeline.py:315-316:
        // a channel with no finite sample is not rendered.
        XCTAssertFalse(series.contains { $0.id == "heart-rate" })
        XCTAssertFalse(series.contains { $0.id == "cadence" })
        XCTAssertTrue(StreamSeries.all(in: ActivityStreams(streams: .init(
            time: nil, power: nil, heartrate: nil, cadence: nil, altitude: nil))).isEmpty)

        let timedChannels = ActivityStreams.Channels(
            time: [12.5, 18.25, 24.75], power: [100, nil, 300],
            heartrate: nil, cadence: nil, altitude: nil
        )
        let timedSeries = StreamSeries.all(in: ActivityStreams(streams: timedChannels))
        XCTAssertEqual(timedSeries[0].points.map(\.time), [12.5, 24.75])
        XCTAssertEqual(timedSeries[0].points.map(\.value), [100, 300])
    }

    func testStreamSeriesPreservesActivityChartOrderAndPalette() {
        let channels = ActivityStreams.Channels(
            time: [0, 1],
            power: [100, 101],
            heartrate: [140, 141],
            cadence: [88, 89],
            altitude: nil
        )
        let series = StreamSeries.all(in: ActivityStreams(streams: channels))

        XCTAssertEqual(series.map(\.id), ["power", "heart-rate", "cadence"])
        XCTAssertEqual(
            series.map(\.title),
            ["Power (W)", "Heart rate (bpm)", "Cadence (rpm)"]
        )
        XCTAssertEqual(series.map(\.color), [Palette.accent, Palette.hr, Palette.ok])
    }

    func testCombinedChartUsesSharedAxesAndLayersAltitudeBehind() {
        let channels = ActivityStreams.Channels(
            time: [0, 60, 120], power: [0, 100, 200], heartrate: [100, 150, 200],
            cadence: [50, 60, 70], altitude: [50, 60, 70]
        )
        let combined = CombinedChartSeries.build(from: ActivityStreams(streams: channels))
        XCTAssertEqual(combined.map(\.id), ["altitude", "power", "heart-rate", "cadence"])
        XCTAssertTrue(combined[0].isBackground)
        XCTAssertEqual(combined[0].points.map(\.scaled), [0, 0.5, 1])   // auto min...max
        XCTAssertEqual(combined[1].points.map(\.scaled), [0, 0.5, 1])   // power axis 0...200
        // HR and cadence share one 0...200 scale, so cadence stays low.
        XCTAssertEqual(combined[2].points.map(\.scaled), [0.5, 0.75, 1])
        XCTAssertEqual(combined[3].points.map(\.scaled), [0.25, 0.3, 0.35])
        XCTAssertEqual(combined[1].points.map(\.minutes), [0, 1, 2])
        let all = StreamSeries.all(in: ActivityStreams(streams: channels))
        XCTAssertEqual(CombinedChartSeries.axis(.power, in: all)?.title, "W")
        XCTAssertEqual(CombinedChartSeries.axis(.shared, in: all)?.title, "bpm / rpm")
        XCTAssertEqual(CombinedChartSeries.axis(.shared, in: all)?.upper, 200)
        XCTAssertEqual(CombinedChartSeries.axis(.shared, in: all)?.scaledTicks.last, 1)
    }

    /// Regression: the cloud time channel is all nil, so x fell back to the
    /// downsampled point index / 60 and an 86-minute ride read 0...25.
    func testEightySixMinuteRideSpansTrueMinutesWithoutTimeChannel() {
        let n = 1_500, duration = 86.0 * 60
        let ramp = (0..<n).map { Double($0 % 300) }
        let channels = ActivityStreams.Channels(
            time: Array(repeating: nil, count: n), power: ramp, heartrate: ramp,
            cadence: nil, altitude: nil)
        let payload = ActivityStreams(streams: channels)
        let series = CombinedChartSeries.build(from: payload, durationSeconds: duration)
        let xs = series[0].points.map(\.minutes)
        XCTAssertEqual(xs.first ?? -1, 0, accuracy: 1e-9)
        XCTAssertEqual(xs.last ?? -1, 86, accuracy: 1e-9)
        XCTAssertEqual(CombinedChartSeries.extent(series), 86, accuracy: 1e-9)
        XCTAssertEqual(ChartTicks.minutes(upTo: 86), [0, 15, 30, 45, 60, 75])
        // 1 Hz, un-downsampled samples agree with the 1 Hz fallback.
        let hz = ActivityStreams(streams: .init(
            time: nil, power: Array(repeating: 100, count: 5_161), heartrate: nil,
            cadence: nil, altitude: nil))
        XCTAssertEqual(CombinedChartSeries.extent(CombinedChartSeries.build(from: hz)), 5_160.0 / 60, accuracy: 1e-9)
        XCTAssertEqual(CombinedChartSeries.extent(CombinedChartSeries.build(from: hz, durationSeconds: 5_160)), 86, accuracy: 1e-9)
    }

    func testChartAxisEdgeCases() {
        XCTAssertEqual(ChartTicks.nice(lower: 0, upper: 312, target: 5).ticks, [0, 100, 200, 300, 400])
        XCTAssertGreaterThan(ChartTicks.nice(lower: 0, upper: 0, target: 5).upper, 0)
        let flat = ChartAxis.zeroBased(for: [0, 0], title: "W")
        XCTAssertGreaterThan(flat.upper, flat.lower)
        let neg = ChartAxis.zeroBased(for: [-20, 100], title: "W")
        XCTAssertLessThanOrEqual(neg.lower, -20)
        XCTAssertGreaterThanOrEqual(neg.upper, 100)
        // Single sample: no divide-by-zero, finite output.
        let one = ActivityStreams(streams: .init(
            time: nil, power: [150], heartrate: nil, cadence: nil, altitude: [10]))
        let built = CombinedChartSeries.build(from: one, durationSeconds: 3_600)
        XCTAssertTrue(built.flatMap(\.points).allSatisfy { $0.scaled.isFinite && $0.minutes.isFinite })
        XCTAssertEqual(ChartTicks.minutes(upTo: 0), [0])
    }

    func testZoneGroupsPreservePositiveRowsLabelsAndPercentages() {
        let zones: JSONValue = .object([
            "power": .object(["zones": .array([
                .object(["label": .string("Recovery"), "seconds": .number(61), "percent": .number(10.25)]),
                .object(["seconds": .number(539), "percent": .number(89.75)]),
                .object(["label": .string("empty"), "seconds": .number(0), "percent": .number(0)])
            ])]),
            "heart_rate": .object(["zones": .array([
                .object(["seconds": .number(600), "percent": .number(100)])
            ])])
        ])
        let groups = ZoneGroup.extract(from: zones)
        XCTAssertEqual(groups.map(\.id), ["power", "heart_rate"])
        XCTAssertEqual(groups[0].rows.map(\.seconds), [61, 539])
        XCTAssertEqual(groups[0].rows.map(\.label), ["Recovery", "Z2"])
        XCTAssertEqual(groups[0].rows.map(\.percent), [10.25, 89.75])
        XCTAssertEqual(groups[1].rows.map(\.seconds), [600])
        XCTAssertEqual(groups[1].rows.map(\.label), ["Z1"])
        XCTAssertEqual(groups[1].rows.map(\.percent), [100])
        XCTAssertTrue(ZoneGroup.extract(from: nil).isEmpty)
    }
}
