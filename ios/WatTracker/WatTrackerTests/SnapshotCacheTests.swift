import XCTest

/// Where last-known data lives, and the properties that make it acceptable for
/// it to live there at all.
///
/// The cache is the rider's training history sitting on disk between launches.
/// The protection class, the backup exclusion and the completeness of
/// `removeAll` are the reasons that is a considered decision rather than a
/// convenience.
final class SnapshotCacheTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("snapshot-cache-\(UUID().uuidString)", isDirectory: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private func sample(revision: Int) -> CachedCollection {
        CachedCollection(
            revision: revision,
            items: [CloudFixtures.item(
                id: "profile", kind: "profile", revision: revision, data: #"{"ftp":240}"#
            )],
            storedAt: Date(timeIntervalSince1970: 1_735_689_600)
        )
    }

    func testARouteSurvivesARestartWithItsCheckpointIntact() throws {
        let written = FileSnapshotCache(directory: directory)
        written.store(sample(revision: 6), for: .dashboard)

        // A different instance, as a relaunch would be.
        let read = FileSnapshotCache(directory: directory)
        let loaded = try XCTUnwrap(read.load(.dashboard))
        XCTAssertEqual(loaded.revision, 6, "the checkpoint is the point of the cache")
        XCTAssertEqual(loaded.items.count, 1)
        XCTAssertNil(read.load(.volume), "routes do not read each other's files")
    }

    func testTheFileIsProtectedAndKeptOutOfBackups() throws {
        let cache = FileSnapshotCache(directory: directory)
        cache.store(sample(revision: 1), for: .dashboard)

        let file = directory.appendingPathComponent("dashboard.json")
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        // Data protection is a device feature; a simulator's file system does
        // not report a class at all. Skipping is the honest outcome there --
        // asserting nil would pin the simulator's behaviour rather than the
        // one that matters, and asserting the class would fail for a reason
        // that has nothing to do with this code.
        guard let protection = attributes[.protectionKey] as? FileProtectionType else {
            throw XCTSkip("this file system reports no protection class")
        }
        XCTAssertEqual(
            protection, .completeUntilFirstUserAuthentication,
            "the same class the device signing key is stored under"
        )

        // A restored cache would put the rider's data on a phone holding no
        // credential to refresh it and no way to be told the old one was
        // revoked. The credential itself is ThisDeviceOnly; the cache matches.
        let values = try directory.resourceValues(forKeys: [.isExcludedFromBackupKey])
        XCTAssertEqual(values.isExcludedFromBackup, true)
    }

    func testClearingRemovesEveryRouteAndNotJustTheKnownOnes() throws {
        let cache = FileSnapshotCache(directory: directory)
        for route in CloudRoute.allCases { cache.store(sample(revision: 3), for: route) }
        // A route this build has retired still has a file, and "clear the
        // cache" has to mean all of it.
        let stray = directory.appendingPathComponent("retired-route.json")
        try Data("{}".utf8).write(to: stray)

        cache.removeAll()

        for route in CloudRoute.allCases {
            XCTAssertNil(cache.load(route), "\(route) survived a clear")
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: stray.path))
    }

    func testAnOversizedCollectionIsNotWrittenAndDoesNotAdvanceTheCheckpoint() throws {
        let cache = FileSnapshotCache(directory: directory)
        cache.store(sample(revision: 2), for: .dashboard)

        let huge = CachedCollection(
            revision: 3,
            items: (0..<400).map { index in
                CloudFixtures.item(
                    id: "bulk-\(index)", kind: "gadget", revision: 3,
                    data: "{\"blob\":\"\(String(repeating: "x", count: 20_000))\"}"
                )
            },
            storedAt: Date()
        )
        cache.store(huge, for: .dashboard)

        // The old checkpoint stands, so the next launch replays that delta
        // rather than skipping past objects it never stored.
        XCTAssertEqual(cache.load(.dashboard)?.revision, 2)
    }

    func testAnUnreadableCacheIsAColdStartRatherThanAnError() throws {
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
        try Data("not json at all".utf8).write(
            to: directory.appendingPathComponent("dashboard.json")
        )
        XCTAssertNil(FileSnapshotCache(directory: directory).load(.dashboard))
    }

    func testNothingSensitiveIsWrittenAlongsideTheObjects() throws {
        // The cache holds objects and a checkpoint. A reader context, a
        // credential id or a subscription key has no field to arrive in here,
        // and this asserts the encoded shape stays that way.
        let cache = FileSnapshotCache(directory: directory)
        cache.store(sample(revision: 5), for: .dashboard)
        let data = try Data(
            contentsOf: directory.appendingPathComponent("dashboard.json")
        )
        let decoded = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        XCTAssertEqual(Set(decoded.keys), ["revision", "items", "storedAt"])
    }
}
