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
            [.dashboard, .volume, .curve]
        )
        XCTAssertEqual(CloudRoute.dashboard.path, "/api/v1/context/dashboard")
    }
}
