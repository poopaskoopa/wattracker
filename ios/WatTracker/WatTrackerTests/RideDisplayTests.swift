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
