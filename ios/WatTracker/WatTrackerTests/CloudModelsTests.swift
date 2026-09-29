import XCTest

final class CloudModelsTests: XCTestCase {
    private struct SharedFixture: Decodable {
        let version: Int
        let kinds: [String]
        let items: [CloudItem]

        private enum CodingKeys: String, CodingKey {
            case version, kinds, items
        }
    }

    private func sharedFixture() throws -> SharedFixture {
        let bundle = Bundle(for: type(of: self))
        let url = try XCTUnwrap(
            bundle.url(forResource: "cloud_objects_v1", withExtension: "json"),
            "The shared cloud object fixture is not in the test bundle."
        )
        return try JSONDecoder().decode(SharedFixture.self, from: Data(contentsOf: url))
    }

    private func decode(_ text: String) throws -> CollectionResponse {
        try JSONDecoder().decode(CollectionResponse.self, from: Data(text.utf8))
    }

    func testEveryPublishedKindDecodesFromTheShapeTheServerSends() throws {
        let fixture = try sharedFixture()
        XCTAssertEqual(fixture.version, 1)
        XCTAssertEqual(fixture.kinds, fixture.kinds.sorted())
        XCTAssertEqual(fixture.kinds.count, 10)
        XCTAssertEqual(Set(fixture.kinds).count, fixture.kinds.count)
        XCTAssertEqual(fixture.items.count, fixture.kinds.count)
        XCTAssertEqual(Set(fixture.items.map { $0.kind.wire }), Set(fixture.kinds))
        XCTAssertEqual(
            Dictionary(grouping: fixture.items, by: { $0.kind.wire }).mapValues(\.count),
            Dictionary(uniqueKeysWithValues: fixture.kinds.map { ($0, 1) })
        )
        for item in fixture.items {
            if case .other = item.payload {
                XCTFail("published kind decoded as .other: \(item.kind.wire)")
            }
            XCTAssertGreaterThan(item.revision, 0)
            XCTAssertFalse(item.deleted)
        }

        let profile = try XCTUnwrap(fixture.items.first { $0.kind == .profile })
        guard case let .profile(profilePayload) = profile.payload else {
            return XCTFail("profile did not decode as one")
        }
        XCTAssertEqual(profilePayload.resolvedFTP, 248)
        XCTAssertEqual(profilePayload.power?.zones?.count, 2)
        XCTAssertNil(profilePayload.power?.zones?[1].max)
        XCTAssertEqual(profilePayload.heartRate?.available, false)

        let stream = try XCTUnwrap(fixture.items.first { $0.kind == .stream })
        guard case let .stream(streamPayload) = stream.payload else {
            return XCTFail("stream did not decode as one")
        }
        let power = try XCTUnwrap(streamPayload.streams.power)
        XCTAssertEqual(power.count, 3)
        XCTAssertNil(power[1], "a recording gap stays a gap")
        XCTAssertNil(streamPayload.streams.cadence, "an unrecorded channel is absent, not empty")
    }

    func testBothProfilePublishersAreUnderstood() throws {
        let skeleton = try decode(
            #"{"items":[{"id":"profile","kind":"profile","revision":1,"data":{"ftp_watts":211.4}}]}"#
        )
        guard case let .profile(profile) = skeleton.items[0].payload else {
            return XCTFail("profile did not decode as one")
        }
        XCTAssertEqual(profile.resolvedFTP, 211.4)
        XCTAssertNil(skeleton.revision, "a non-mobile route carries no checkpoint")
    }

    func testAnUnknownKindSurvivesADecodeAndEncodeUnchanged() throws {
        let text = """
        {"id":"gadget-1","kind":"gadget","revision":6,\
        "data":{"nested":{"list":[1,2,3],"flag":true,"nothing":null},"name":"x"}}
        """
        let item = try JSONDecoder().decode(CloudItem.self, from: Data(text.utf8))
        XCTAssertEqual(item.kind, .other("gadget"))
        guard case let .other(value) = item.payload else {
            return XCTFail("an unknown kind must keep its data")
        }
        XCTAssertEqual(value["name"]?.stringValue, "x")
        let round = try JSONDecoder().decode(
            CloudItem.self, from: try JSONEncoder().encode(item)
        )
        XCTAssertEqual(round, item)
    }

    func testAnUnknownKindDoesNotTakeThePageDownWithIt() throws {
        let response = try decode("""
        {"items":[
          {"id":"gadget-1","kind":"gadget","revision":6,"data":{"whatever":1}},
          {"id":"profile","kind":"profile","revision":6,"data":{"ftp":250}}
        ],"revision":6,"next_cursor":null}
        """)
        XCTAssertEqual(response.items.count, 2)
        XCTAssertEqual(response.items[1].kind, .profile)
    }

    func testATombstoneCarriesNoPayloadAndIsNotMistakenForAnObject() throws {
        let response = try decode("""
        {"items":[{"id":"training-state","kind":"training_state","revision":9,
                   "data":{},"deleted":true}],"revision":9,"next_cursor":null}
        """)
        let item = try XCTUnwrap(response.items.first)
        XCTAssertTrue(item.deleted)
        XCTAssertEqual(item.payload, .tombstone)
        XCTAssertEqual(item.kind, .trainingState)
    }

    func testACachedCollectionRoundTripsThroughItsOwnEncoder() throws {
        let original = CachedCollection(
            revision: 12,
            items: [
                CloudFixtures.item(id: "profile", kind: "profile", revision: 12, data: #"{"ftp":244}"#),
                CloudFixtures.item(id: "gadget-1", kind: "gadget", revision: 12, data: #"{"a":[1,null]}"#),
            ],
            storedAt: Date(timeIntervalSince1970: 1_735_689_600)
        )
        let decoded = try JSONDecoder().decode(
            CachedCollection.self, from: try JSONEncoder().encode(original)
        )
        XCTAssertEqual(decoded, original)
    }

    func testTheRoutesThatServeDeltasAreExactlyTheMobileOnes() {
        XCTAssertEqual(
            Set(CloudRoute.allCases.filter(\.servesDeltas)),
            [.dashboard, .volume, .curve, .activities, .calendar]
        )
        XCTAssertEqual(CloudRoute.dashboard.path, "/api/v1/context/dashboard")
    }

    // MARK: - The profile the desktop really publishes
    //
    // Generated from `zones.zone_ranges(250.0, POWER_ZONES)` and
    // `zone_ranges(180, HR_ZONES)`, wrapped the way `snapshot._profile_object`
    // wraps them.  `pct` is the desktop's display string ("<56%", "56–75%"
    // with an en dash, ">150%"), `min`/`max` are ints, and the top zone's
    // `max` is null.  The shared vector carries a numeric `pct`, which is why
    // the Dashboard's decode failure on real data was never caught.

    private static let realPowerZones = """
    [{"label":"Z1","name":"Active recovery","pct":"<56%","min":0,"max":139,"range":"≤139"},\
    {"label":"Z2","name":"Endurance","pct":"56–75%","min":140,"max":189,"range":"140–189"},\
    {"label":"Z3","name":"Tempo","pct":"76–90%","min":190,"max":227,"range":"190–227"},\
    {"label":"Z4","name":"Threshold","pct":"91–105%","min":228,"max":264,"range":"228–264"},\
    {"label":"Z5","name":"VO₂ max","pct":"106–120%","min":265,"max":302,"range":"265–302"},\
    {"label":"Z6","name":"Anaerobic","pct":"121–150%","min":303,"max":377,"range":"303–377"},\
    {"label":"Z7","name":"Neuromuscular","pct":">150%","min":378,"max":null,"range":"≥378"}]
    """

    private static let realHeartRateZones = """
    [{"label":"Z1","name":"Recovery","pct":"50–59%","min":0,"max":107,"range":"≤107"},\
    {"label":"Z2","name":"Easy","pct":"60–69%","min":108,"max":125,"range":"108–125"},\
    {"label":"Z3","name":"Aerobic","pct":"70–79%","min":126,"max":143,"range":"126–143"},\
    {"label":"Z4","name":"Threshold","pct":"80–89%","min":144,"max":161,"range":"144–161"},\
    {"label":"Z5","name":"Maximum","pct":"90–100%+","min":162,"max":null,"range":"≥162"}]
    """

    private static func realProfileData(
        power: String = realPowerZones, heartRate: String = realHeartRateZones
    ) -> String {
        """
        {"display_name":"rider","ftp":250.0,\
        "power":{"available":true,"value":250.0,"source":"Manual Training FTP setting","zones":\(power)},\
        "heart_rate":{"available":true,"value":180,"source":"Manual HRmax","zones":\(heartRate)},\
        "zones":{"power":\(power),"heart_rate":\(heartRate)},\
        "weight_kg":70.5,"weight_date":"2026-09-20","weight_source":"manual"}
        """
    }

    /// `pct` read through interpolation so these tests still compile when the
    /// field is mutated back to a number, and fail at decode instead.
    private func pctText(_ zone: Zone) -> String? {
        zone.pct.map { "\($0)" }
    }

    func testTheProfileTheDesktopReallyPublishesDecodes() throws {
        let item = try JSONDecoder().decode(CloudItem.self, from: Data("""
        {"id":"profile","kind":"profile","revision":3,"data":\(Self.realProfileData())}
        """.utf8))
        guard case let .profile(profile) = item.payload else {
            return XCTFail("profile did not decode as one")
        }
        XCTAssertEqual(profile.resolvedFTP, 250)
        let power = try XCTUnwrap(profile.power?.zones)
        XCTAssertEqual(power.map(pctText), [
            "<56%", "56–75%", "76–90%", "91–105%", "106–120%", "121–150%", ">150%",
        ], "pct is the desktop's display string, passed through as-is")
        XCTAssertEqual(power[0].min, 0)
        XCTAssertEqual(power[1].max, 189)
        XCTAssertNil(power[6].max, "the open-ended top zone has no max")
        XCTAssertEqual(power[6].range, "≥378")
        let heartRate = try XCTUnwrap(profile.heartRate?.zones)
        XCTAssertEqual(heartRate.count, 5)
        XCTAssertEqual(pctText(heartRate[4]), "90–100%+")
        XCTAssertEqual(profile.heartRate?.value, 180)
    }

    func testADashboardPageWithTheRealProfileDecodes() throws {
        let response = try decode("""
        {"items":[
          {"id":"profile","kind":"profile","revision":7,"data":\(Self.realProfileData())},
          {"id":"training-state","kind":"training_state","revision":7,
           "data":{"ftp":250.0,"cp":262.4,"wprime":18450.2,"ctl":61.3,"atl":70.1,
                   "tsb":-8.8,"decoupling":null}},
          {"id":"ftp-history-2026-09-01","kind":"ftp_history","revision":7,
           "data":{"date":"2026-09-01","ftp_watts":250.0,"source":"manual"}},
          {"id":"load-point-2026-09-27","kind":"load_point","revision":7,
           "data":{"date":"2026-09-27","tss":84.2,"ctl":61.3,"atl":70.1,"tsb":-8.8}},
          {"id":"curve","kind":"curve","revision":7,
           "data":{"measured":[{"t":1,"power":912.0},{"t":60,"power":401.5}],
                   "all_time":[{"t":1,"power":950.0}],"last_ride":[],
                   "model":[{"t":60,"power":569.9}],"cp":262.4,"wprime":18450.2}}
        ],"revision":7,"next_cursor":null}
        """)
        XCTAssertEqual(response.items.map(\.kind), [
            .profile, .trainingState, .ftpHistory, .loadPoint, .curve,
        ])
        guard case let .curve(curve) = response.items[4].payload else {
            return XCTFail("curve did not decode as one")
        }
        XCTAssertEqual(curve.measured?.first?.t, 1)
        guard case let .profile(profile) = response.items[0].payload else {
            return XCTFail("profile did not decode as one")
        }
        XCTAssertEqual(profile.power?.zones?.count, 7)
    }

    func testAZoneFieldOfAnUnexpectedTypeIsNilNotAFailedPage() throws {
        let zones = """
        [{"label":"Z1","name":"Active recovery","pct":56,"min":0,"max":139,"range":"≤139"},\
        {"label":"Z2","name":"Endurance","pct":"56–75%","min":"140","max":{"w":189},"range":7}]
        """
        let response = try decode("""
        {"items":[{"id":"profile","kind":"profile","revision":2,
                   "data":\(Self.realProfileData(power: zones))}],
         "revision":2,"next_cursor":null}
        """)
        guard case let .profile(profile) = response.items[0].payload else {
            return XCTFail("profile did not decode as one")
        }
        let power = try XCTUnwrap(profile.power?.zones)
        XCTAssertNil(power[0].pct, "a numeric pct is not the display string; it reads as absent")
        XCTAssertEqual(power[0].max, 139)
        XCTAssertEqual(pctText(power[1]), "56–75%")
        XCTAssertNil(power[1].min)
        XCTAssertNil(power[1].max)
        XCTAssertNil(power[1].range)
        XCTAssertEqual(profile.heartRate?.zones?.count, 5)
    }

    func testCalendarWorkoutProfileIsOptionalAndLenient() throws {
        let response = try decode("""
        {"items":[{"id":"calendar-day-2026-09-29","kind":"calendar_day","revision":4,
          "data":{"date":"2026-09-29","workouts":[
            {"name":"Threshold","profile":[
              {"start":0,"end":600,"duration_s":600,"target_start":1.0,
               "target_end":1.0,"kind":"steadystate","label":"Steady block"}
            ]},
            {"name":"Legacy"},
            {"name":"Malformed","profile":"not-an-array"}
          ]}}],"revision":4,"next_cursor":null}
        """)
        guard case let .calendarDay(day) = response.items[0].payload else {
            return XCTFail("calendar day did not decode")
        }
        let workouts = try XCTUnwrap(day.workouts)
        XCTAssertEqual(workouts.count, 3)
        let blocks = CalendarWorkoutProfileDecoder.blocks(from: workouts[0])
        XCTAssertEqual(blocks.count, 1)
        XCTAssertEqual(blocks[0].durationS, 600)
        XCTAssertEqual(blocks[0].targetStart, 1.0)
        XCTAssertEqual(blocks[0].kind, "steadystate")
        XCTAssertTrue(CalendarWorkoutProfileDecoder.blocks(from: workouts[1]).isEmpty)
        XCTAssertTrue(CalendarWorkoutProfileDecoder.blocks(from: workouts[2]).isEmpty)
    }

    func testLeniencyStopsAtTheEnvelope() {
        XCTAssertThrowsError(try decode("""
        {"items":[{"id":"profile","kind":"profile","revision":"7",
                   "data":\(Self.realProfileData())}],"revision":7,"next_cursor":null}
        """), "a revision of the wrong type is still a malformed response")
    }
}
