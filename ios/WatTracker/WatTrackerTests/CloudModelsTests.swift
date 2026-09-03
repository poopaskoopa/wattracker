import XCTest

/// The envelope, and the two decoding rules that are not stylistic.
///
/// A wrong model here is not a crash: it is a screen that renders nothing for a
/// rider whose snapshot is perfectly good, or -- worse -- an object silently
/// dropped from a delta the server will never send again.
final class CloudModelsTests: XCTestCase {
    private func decode(_ text: String) throws -> CollectionResponse {
        try JSONDecoder().decode(CollectionResponse.self, from: Data(text.utf8))
    }

    func testEveryPublishedKindDecodesFromTheShapeTheServerSends() throws {
        // These are the shapes `wattracker/cloud/snapshot.py` builds, with the
        // nulls it really emits: `_safe_data` turns every non-finite float into
        // one, and a rider with no FTP, no HR data and no weight history gets
        // them everywhere.
        let response = try decode("""
        {"items":[
          {"id":"profile","kind":"profile","revision":4,"data":{
            "display_name":"rider","ftp":248.0,
            "power":{"available":true,"value":248.0,"source":"Manual Training FTP setting",
                     "zones":[{"label":"Z1","name":"Active Recovery","pct":0.55,
                               "min":0,"max":136,"range":"0-136"},
                              {"label":"Z6","name":"Anaerobic","pct":1.2,
                               "min":298,"max":null,"range":"\u{2265}298"}]},
            "heart_rate":{"available":false,"value":null,
                          "source":"Insufficient FIT heart-rate data","zones":[]},
            "weight_kg":null,"weight_date":null,"weight_source":null}},
          {"id":"training-state","kind":"training_state","revision":4,"data":{
            "ftp":248.0,"cp":null,"wprime":null,"ctl":42.5,"atl":38,"tsb":4.5,
            "decoupling":null}},
          {"id":"load-point-2026-01-02","kind":"load_point","revision":4,"data":{
            "date":"2026-01-02","tss":95.0,"ctl":42.5,"atl":38.0,"tsb":4.5}},
          {"id":"curve","kind":"curve","revision":4,"data":{
            "measured":[{"t":60,"power":410.0}],"all_time":[{"t":60,"power":455.0}],
            "last_ride":[],"model":[{"t":60,"power":402.1}],"cp":268.0,"wprime":18000.0}},
          {"id":"volume-week-2026-01-05","kind":"volume_week","revision":4,"data":{
            "week_start":"2026-01-05","hours":8.25,"tss":540.5,"distance_km":210.4,
            "calories":6100}},
          {"id":"ftp-history-2025-12-01","kind":"ftp_history","revision":4,"data":{
            "date":"2025-12-01","ftp_watts":240.0,"source":"ramp test"}},
          {"id":"activity-17","kind":"activity","revision":17,"data":{
            "id":17,"start_time":"2026-01-02T18:04:00","duration_s":3600,
            "distance_m":32100.5,"avg_power":211,"avg_hr":142,"np":225,
            "if_":0.9,"tss":81.0,"rpe":6}},
          {"id":"activity-detail-17","kind":"activity_detail","revision":17,"data":{
            "id":17,"start_time":"2026-01-02T18:04:00","duration_s":3600,
            "weight_kg":72.0,"weight_source":"settings","weight_date":"2026-01-01",
            "zones":{"power":[{"label":"Z2","seconds":1800}]}}},
          {"id":"stream-17","kind":"stream","revision":17,"data":{
            "streams":{"time":[0,1,2],"power":[180,null,205],"heartrate":[120,121,123]}}},
          {"id":"calendar-day-2026-01-02","kind":"calendar_day","revision":4,"data":{
            "date":"2026-01-02","race":null,"ooto":false,"phase":"Build",
            "workouts":[{"id":9,"title":"Sweet spot","anything":"else"}],
            "activities":[]}}
        ],"revision":4,"next_cursor":null}
        """)

        XCTAssertEqual(response.items.count, 10)
        XCTAssertEqual(response.revision, 4)
        XCTAssertNil(response.nextCursor)
        XCTAssertEqual(
            response.items.map(\.kind),
            [.profile, .trainingState, .loadPoint, .curve, .volumeWeek, .ftpHistory,
             .activity, .activityDetail, .stream, .calendarDay]
        )

        guard case let .profile(profile) = response.items[0].payload else {
            return XCTFail("profile did not decode as one")
        }
        XCTAssertEqual(profile.resolvedFTP, 248)
        XCTAssertEqual(profile.power?.zones?.count, 2)
        // The open-ended top zone has no maximum, and that is a value rather
        // than a missing field: a model demanding it would fail every rider.
        XCTAssertNil(profile.power?.zones?[1].max)
        XCTAssertEqual(profile.heartRate?.available, false)

        guard case let .stream(stream) = response.items[8].payload else {
            return XCTFail("stream did not decode as one")
        }
        let power = try XCTUnwrap(stream.streams.power)
        XCTAssertEqual(power.count, 3)
        XCTAssertNil(power[1], "a recording gap stays a gap")
        XCTAssertNil(stream.streams.cadence, "an unrecorded channel is absent, not empty")
    }

    /// The walking skeleton's publisher writes `ftp_watts`; the derived
    /// snapshot writes `ftp`. One type reads both, so the debug round-trip and
    /// the dashboard cannot disagree about what a profile is.
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
        // The delta never resends. An object this build cannot model has to
        // come back out of the cache byte-equivalent, or a rider who updates
        // the app finds a hole where it used to be.
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
        // The server sends `data: {}` on a deletion. Decoding that as a
        // profile would throw on the first required field.
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
                CloudFixtures.item(
                    id: "profile", kind: "profile", revision: 12, data: #"{"ftp":244}"#
                ),
                CloudFixtures.item(
                    id: "gadget-1", kind: "gadget", revision: 12, data: #"{"a":[1,null]}"#
                ),
            ],
            storedAt: Date(timeIntervalSince1970: 1_735_689_600)
        )
        let decoded = try JSONDecoder().decode(
            CachedCollection.self, from: try JSONEncoder().encode(original)
        )
        XCTAssertEqual(decoded, original)
    }

    func testTheRoutesThatServeDeltasAreExactlyTheMobileOnes() {
        // `api.py` passes `mobile: True` for these four and nothing else.
        // Sending `since=` anywhere else would be asking a question the route
        // does not answer, and reading a checkpoint out of the reply would
        // mistake a truncation for one.
        XCTAssertEqual(
            Set(CloudRoute.allCases.filter(\.servesDeltas)),
            [.dashboard, .volume, .curve, .activities]
        )
        XCTAssertEqual(CloudRoute.dashboard.path, "/api/v1/context/dashboard")
    }
}
