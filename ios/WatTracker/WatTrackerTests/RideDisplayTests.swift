import XCTest
@testable import WatTracker

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

    func testFormattingHandlesHourBoundaryDistanceAndMissingValues() {
        // Desktop rounds the same duration in wattracker/calendarfeed.py:268-278.
        XCTAssertEqual(RideFormatting.duration(3599.5), "1:00:00")
        XCTAssertEqual(RideFormatting.duration(3601), "1:00:01")
        // The desktop detail card divides meters by 1,000 and formats one
        // decimal place in wattracker/web/templates/activity_detail.html:13.
        XCTAssertEqual(RideFormatting.distance(12345), "12.3 km")
        XCTAssertEqual(RideFormatting.distance(nil), "—")
        XCTAssertEqual(RideFormatting.watts(0), "0 W")
        XCTAssertEqual(RideFormatting.watts(nil), "—")
    }

    func testStreamsDropGapsAndUseIndexWhenTimeIsMissing() {
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
    }

    func testZoneGroupsPreservePositiveRowsAndTotals() {
        let zones: JSONValue = .object([
            "power": .object(["zones": .array([
                .object(["label": .string("Z1"), "seconds": .number(60), "percent": .number(10)]),
                .object(["label": .string("Z2"), "seconds": .number(540), "percent": .number(90)]),
                .object(["label": .string("empty"), "seconds": .number(0)])
            ])]),
            "heart_rate": .object(["zones": .array([
                .object(["label": .string("Z1"), "seconds": .number(600), "percent": .number(100)])
            ])])
        ])
        let groups = ZoneGroup.extract(from: zones)
        XCTAssertEqual(groups.map(\.id), ["power", "heart_rate"])
        // Desktop assigns elapsed samples in wattracker/analysis/zones.py:325-369
        // (`time_in_zones`), so each group's rows must sum to the ride duration.
        XCTAssertEqual(groups[0].rows.map(\.seconds).reduce(0, +), 600)
        XCTAssertEqual(groups[1].rows.map(\.seconds).reduce(0, +), 600)
        XCTAssertTrue(ZoneGroup.extract(from: nil).isEmpty)
    }
}
