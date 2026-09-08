import XCTest

final class MobileScreenDataTests: XCTestCase {
    private let asOf = Date(timeIntervalSince1970: 1_735_689_600)

    private func date(year: Int, month: Int, day: Int) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar.date(
            from: DateComponents(year: year, month: month, day: day)
        )!
    }

    private func snapshot(
        route: CloudRoute,
        items: [CloudItem],
        source: CloudSnapshot.Source = .network
    ) -> CloudSnapshot {
        CloudSnapshot(
            route: route,
            revision: 7,
            items: items,
            source: source,
            asOf: asOf
        )
    }

    private func calendarItem(
        id: String,
        date: String,
        workouts: String = "[]",
        activities: String = "[]",
        ooto: Bool = false,
        phase: String? = nil,
        race: String = "null",
        part: Int? = nil,
        parts: Int? = nil
    ) -> CloudItem {
        var fields = [
            "\"date\":\"\(date)\"",
            "\"race\":\(race)",
            "\"ooto\":\(ooto)",
            "\"phase\":\(phase.map { "\"\($0)\"" } ?? "null")",
            "\"workouts\":\(workouts)",
            "\"activities\":\(activities)",
        ]
        if let part {
            fields.append("\"part\":\(part)")
        }
        if let parts {
            fields.append("\"parts\":\(parts)")
        }
        return CloudFixtures.item(
            id: id,
            kind: "calendar_day",
            revision: 1,
            data: "{\(fields.joined(separator: ","))}"
        )
    }

    func testCalendarChunksMergeByDateAndPreserveDayFlags() {
        let first = calendarItem(
            id: "calendar-day-2026-01-02-part-1",
            date: "2026-01-02",
            workouts: "[{\"id\":1,\"name\":\"Sweet spot\",\"completed_activity_id\":22}]",
            phase: "Build",
            part: 1,
            parts: 2
        )
        let second = calendarItem(
            id: "calendar-day-2026-01-02-part-2",
            date: "2026-01-02",
            activities: "[{\"id\":23,\"start_time\":\"2026-01-02T08:00:00Z\"}]",
            ooto: true,
            race: "{\"name\":\"Winter Fondo\"}",
            part: 2,
            parts: 2
        )
        let invalid = calendarItem(
            id: "calendar-day-invalid",
            date: "2026-99-99",
            phase: "Ignored"
        )

        let linkedActivity = CloudFixtures.item(
            id: "activity-22",
            kind: "activity",
            revision: 22,
            data: """
            {"id":22,"start_time":"2026-01-02T10:00:00Z",
             "duration_s":3600,"distance_m":30000,"tss":70}
            """
        )
        let data = CalendarData(
            snapshot: snapshot(route: .calendar, items: [second, invalid, first]),
            activitySnapshot: snapshot(route: .activities, items: [linkedActivity])
        )

        XCTAssertEqual(data.days.count, 1)
        let day = data.days[0]
        XCTAssertEqual(day.dateISO, "2026-01-02")
        XCTAssertEqual(day.phase, "Build")
        XCTAssertTrue(day.ooto)
        XCTAssertEqual(day.workouts.count, 1)
        XCTAssertEqual(day.activities.count, 2)
        XCTAssertEqual(day.race?["name"]?.stringValue, "Winter Fondo")
        XCTAssertEqual(
            Set(day.activities.compactMap { $0["id"]?.doubleValue }),
            [22, 23]
        )
    }

    func testCalendarMonthUsesMondayColumnsAndLeapDays() {
        let leap = CalendarMonth(year: 2024, month: 2)
        XCTAssertEqual(leap.dayCount, 29)
        XCTAssertEqual(leap.leadingEmptyDays, 3)

        let sunday = CalendarMonth(year: 2024, month: 9)
        XCTAssertEqual(sunday.leadingEmptyDays, 6)
        XCTAssertEqual(sunday.isoDate(day: 30), "2024-09-30")
        XCTAssertEqual(CalendarMonth.fromISODate("2024-09-30"), sunday)
        XCTAssertNil(CalendarMonth.fromISODate("2024-02-30"))
    }

    func testVolumeFillsMissingMondayBucketsAndCalculatesSummaries() {
        let starts = [
            "2025-12-01", "2025-12-08", "2025-12-15", "2025-12-22",
            "2025-12-29", "2026-01-05", "2026-01-19", "2026-01-26",
        ]
        let items = starts.enumerated().map { index, start in
            CloudItem(
                id: "volume-week-\(start)",
                kind: .volumeWeek,
                revision: index + 1,
                payload: .volumeWeek(
                    VolumeWeek(
                        weekStart: start,
                        hours: Double(index + 1),
                        tss: Double((index + 1) * 10),
                        distanceKm: Double((index + 1) * 5),
                        calories: Double((index + 1) * 100)
                    )
                )
            )
        }

        let data = VolumeData(snapshot: snapshot(route: .volume, items: items))
        XCTAssertEqual(data.denseWeeks.map(\.weekStart), [
            "2025-12-01", "2025-12-08", "2025-12-15", "2025-12-22",
            "2025-12-29", "2026-01-05", "2026-01-12", "2026-01-19",
            "2026-01-26",
        ])
        XCTAssertEqual(data.denseWeeks[6].hours, 0)
        XCTAssertEqual(data.weeks(for: .last4).count, 4)
        XCTAssertEqual(
            data.weeks(
                for: .yearToDate,
                referenceDate: date(year: 2026, month: 9, day: 7)
            ).map(\.weekStart),
            [
                "2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26",
            ]
        )
        XCTAssertEqual(data.summary(for: .last4).hours, 21)
        XCTAssertEqual(data.summary(for: .last4).tss, 210)
        XCTAssertEqual(data.summary(for: .last4).distanceKm, 105)
        XCTAssertEqual(data.summary(for: .last4).calories, 2100)
        XCTAssertEqual(
            try XCTUnwrap(data.change(for: .hours, range: .last4)),
            21.0 / 14.0 - 1.0,
            accuracy: 0.0001
        )
        XCTAssertEqual(data.source, .network)
        XCTAssertEqual(data.asOf, asOf)
    }

    func testYearToDateUsesReferenceYearRatherThanNewestRide() {
        let item = CloudItem(
            id: "volume-week-2025-12-22",
            kind: .volumeWeek,
            revision: 1,
            payload: .volumeWeek(
                VolumeWeek(
                    weekStart: "2025-12-22",
                    hours: 10,
                    tss: 100,
                    distanceKm: 50,
                    calories: 500
                )
            )
        )
        let data = VolumeData(
            snapshot: snapshot(route: .volume, items: [item])
        )

        let reference = date(year: 2026, month: 9, day: 7)
        XCTAssertTrue(data.weeks(for: .yearToDate, referenceDate: reference).isEmpty)
        XCTAssertEqual(
            data.summary(for: .yearToDate, referenceDate: reference).hours,
            0
        )
    }
}
