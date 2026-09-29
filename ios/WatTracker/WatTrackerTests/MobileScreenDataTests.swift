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

    func testCalendarWorkoutStepsUseTheCurrentFTPInTheirDescription() {
        let step = CalendarWorkoutProfileBlock(
            start: 0,
            end: 600,
            durationS: 600,
            targetStart: 1,
            targetEnd: 1,
            kind: "steadystate",
            label: "Steady block",
            text: nil,
            free: false
        )

        XCTAssertEqual(
            CalendarWorkoutFormatting.stepDescription(step, ftp: 250),
            "10 min at 250 W (100% FTP)"
        )
    }

    func testCalendarIntervalStepsKeepTheDesktopDescriptionAndTargets() {
        let steps = [
            CalendarWorkoutProfileBlock(
                start: 0, end: 60, durationS: 60,
                targetStart: 0.93, targetEnd: 0.93,
                kind: "intervals", label: "2 x 1min on / 1min easy",
                text: nil, free: false, segmentIndex: 0
            ),
            CalendarWorkoutProfileBlock(
                start: 60, end: 120, durationS: 60,
                targetStart: 0.55, targetEnd: 0.55,
                kind: "intervals", label: "2 x 1min on / 1min easy",
                text: nil, free: false, segmentIndex: 0
            ),
            CalendarWorkoutProfileBlock(
                start: 120, end: 180, durationS: 60,
                targetStart: 0.93, targetEnd: 0.93,
                kind: "intervals", label: "2 x 1min on / 1min easy",
                text: nil, free: false, segmentIndex: 0
            ),
        ]

        XCTAssertEqual(
            CalendarWorkoutFormatting.stepDescriptions(steps, ftp: 250),
            ["2 x 1min on / 1min easy at 232 W / 138 W (93% / 55% FTP)"]
        )
    }

    func testCalendarTempoProgressionKeepsDistinctStepsDistinct() {
        let label = "1 x 9min on / 4min easy"
        let steps = [0.78, 0.55, 0.80, 0.82].enumerated().map {
            CalendarWorkoutProfileBlock(
                start: Double($0.offset * 60),
                end: Double(($0.offset + 1) * 60),
                durationS: 60,
                targetStart: $0.element,
                targetEnd: $0.element,
                kind: "intervals",
                label: label,
                text: nil,
                free: false,
                segmentIndex: $0.offset
            )
        }

        XCTAssertEqual(
            CalendarWorkoutFormatting.stepDescriptions(steps, ftp: 250),
            [
                "1 x 9min on / 4min easy at 195 W (78% FTP)",
                "1 x 9min on / 4min easy at 138 W (55% FTP)",
                "1 x 9min on / 4min easy at 200 W (80% FTP)",
                "1 x 9min on / 4min easy at 205 W (82% FTP)",
            ]
        )
    }

    func testCalendarLegacyIntervalsStillGroupWithoutSegmentMarkers() {
        let label = "2 x 1min on / 1min easy"
        let steps = [0.93, 0.55, 0.93].enumerated().map {
            CalendarWorkoutProfileBlock(
                start: Double($0.offset * 60),
                end: Double(($0.offset + 1) * 60),
                durationS: 60,
                targetStart: $0.element,
                targetEnd: $0.element,
                kind: "intervals",
                label: label,
                text: nil,
                free: false
            )
        }

        XCTAssertEqual(
            CalendarWorkoutFormatting.stepDescriptions(steps, ftp: 250),
            ["2 x 1min on / 1min easy at 232 W / 138 W (93% / 55% FTP)"]
        )
    }

    func testCalendarChartFallsBackToPercentFTPWithoutAnFTP() {
        XCTAssertEqual(
            CalendarWorkoutFormatting.chartTarget(0.78, ftp: nil),
            78
        )
        XCTAssertEqual(
            CalendarWorkoutFormatting.chartTarget(0.78, ftp: 250),
            195
        )
    }

    func testCalendarCarriesFTPAlongsideAnOptionalWorkoutProfile() {
        let item = calendarItem(
            id: "calendar-day-2026-01-03",
            date: "2026-01-03",
            workouts: """
            [{"name":"Threshold","profile":[{"start":0,"end":600,
              "duration_s":600,"target_start":1,"target_end":1,
              "kind":"steadystate"}]}]
            """
        )
        let data = CalendarData(
            snapshot: snapshot(route: .calendar, items: [item]),
            ftp: 250
        )

        let day = data.entry(for: "2026-01-03")
        XCTAssertEqual(day?.currentFTP, 250)
        let profile = day.flatMap { CalendarWorkoutProfileDecoder.blocks(from: $0.workouts[0]) }
        XCTAssertEqual(profile?.first?.targetStart, 1)
    }

    func testCalendarFTPUsesTrainingStateBeforeProfileFallback() {
        let training = CloudFixtures.item(
            id: "training-state",
            kind: "training_state",
            revision: 2,
            data: "{\"ftp\":260}"
        )
        let profile = CloudFixtures.item(
            id: "profile",
            kind: "profile",
            revision: 2,
            data: "{\"ftp\":250}"
        )

        XCTAssertEqual(
            CalendarData.currentFTP(
                from: snapshot(route: .dashboard, items: [profile, training])
            ),
            260
        )
        XCTAssertEqual(
            CalendarData.currentFTP(
                from: snapshot(route: .dashboard, items: [profile])
            ),
            250
        )
        let invalidTraining = CloudFixtures.item(
            id: "training-state",
            kind: "training_state",
            revision: 2,
            data: "{\"ftp\":0}"
        )
        XCTAssertEqual(
            CalendarData.currentFTP(
                from: snapshot(route: .dashboard, items: [profile, invalidTraining])
            ),
            250
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

    func testChangeIsWithheldUntilAFullPriorWindowExists() {
        let data = flatVolumeData(weekCount: 5)

        XCTAssertEqual(data.weeks(for: .last4).count, 4)
        XCTAssertEqual(data.summary(for: .last4).hours, 20)
        XCTAssertTrue(data.previousWeeks(for: .last4).isEmpty)
        for metric in VolumeMetric.allCases {
            XCTAssertNil(data.change(for: metric, range: .last4), "\(metric)")
        }
    }

    func testChangeAppearsExactlyWhenTwoFullWindowsExist() throws {
        for (range, count) in [(VolumeRange.last4, 4), (.last12, 12)] {
            let before = flatVolumeData(weekCount: count * 2 - 1)
            let complete = flatVolumeData(weekCount: count * 2)

            XCTAssertEqual(before.weeks(for: range).count, count)
            XCTAssertTrue(before.previousWeeks(for: range).isEmpty)
            XCTAssertEqual(complete.weeks(for: range).count, count)
            XCTAssertEqual(complete.previousWeeks(for: range).count, count)
            for metric in VolumeMetric.allCases {
                XCTAssertNil(before.change(for: metric, range: range), "\(range), \(metric)")
                XCTAssertEqual(
                    try XCTUnwrap(complete.change(for: metric, range: range)),
                    0,
                    accuracy: 0.0001,
                    "\(range), \(metric)"
                )
            }
        }
    }

    private func flatVolumeData(weekCount: Int) -> VolumeData {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let firstMonday = date(year: 2025, month: 1, day: 6)
        let items = (0..<weekCount).map { index in
            let monday = calendar.date(byAdding: .day, value: index * 7, to: firstMonday)!
            let components = calendar.dateComponents([.year, .month, .day], from: monday)
            let start = String(
                format: "%04d-%02d-%02d", components.year!, components.month!, components.day!
            )
            return CloudItem(
                id: "volume-week-\(start)",
                kind: .volumeWeek,
                revision: index + 1,
                payload: .volumeWeek(
                    VolumeWeek(
                        weekStart: start, hours: 5, tss: 100,
                        distanceKm: 50, calories: 1000
                    )
                )
            )
        }
        return VolumeData(snapshot: snapshot(route: .volume, items: items))
    }
}
