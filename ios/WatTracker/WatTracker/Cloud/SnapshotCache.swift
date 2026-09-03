import Foundation

/// One route's last-known objects, and the checkpoint they were read at.
///
/// The revision is the server's, copied back verbatim and never computed here.
/// It is the only thing that makes the delta correct: `since=` is answered
/// against the scope's own counter, and a client that invented a checkpoint --
/// by counting objects, by taking a maximum, by using a timestamp -- would ask
/// for a window the server never promised and silently miss whatever fell
/// outside it.
struct CachedCollection: Codable, Sendable, Equatable {
    let revision: Int
    let items: [CloudItem]
    let storedAt: Date
}

/// Where last-known data lives between launches.
protocol SnapshotCache: Sendable {
    func load(_ route: CloudRoute) -> CachedCollection?
    func store(_ collection: CachedCollection, for route: CloudRoute)
    /// Forget everything.  Called on revocation, where it is the point.
    func removeAll()
}

/// The shipping cache: one JSON file per route, protected, unbacked-up.
///
/// **Why not `UserDefaults`.** A plist in `Library/Preferences` is readable by
/// anything that can read the container, is copied into every backup, and has
/// no file-protection class of its own.  This is the rider's training history;
/// it belongs in a file with a protection class, and nothing here goes into
/// defaults at any size.
///
/// **Why not the keychain either.** The keychain is for the small secrets that
/// authorize access -- the signing key and the device credential.  Megabytes of
/// derived training data would be a misuse of it, and would not be more secret
/// for it: an attacker who can read a `completeUntilFirstUserAuthentication`
/// file on an unlocked device can equally read an
/// `AfterFirstUnlockThisDeviceOnly` keychain item.
///
/// **Why `completeUntilFirstUserAuthentication` rather than `complete`.** It is
/// the same class the device signing key uses, deliberately: a cache the app
/// could not read at exactly the moments the key is usable would just be a
/// cache that is empty when it matters.  `complete` would also make the cache
/// unreadable to any future background refresh on a locked phone.
///
/// **Why it is excluded from backups.** The device credential cannot be
/// restored onto another device -- `DeviceKeyStore` writes it
/// `ThisDeviceOnly`. A cache that *did* restore would put the rider's training
/// data on a new phone that has no credential to refresh it and no way to
/// clear it if the old phone were revoked: data outliving the authorization
/// that fetched it.
struct FileSnapshotCache: SnapshotCache {
    /// A hard ceiling per route, well above a real dashboard (a handful of
    /// objects) and still far below anything that would embarrass the file
    /// system.  A response over it is served to the caller but not written:
    /// the cached checkpoint stays where it was, so the next launch replays
    /// the same delta rather than skipping it.  At-least-once is the contract;
    /// silently advancing past unstored data would break it.
    static let maximumBytesPerRoute = 4 * 1024 * 1024

    let directory: URL

    init(directory: URL? = nil) {
        self.directory = directory ?? Self.defaultDirectory()
    }

    static func defaultDirectory() -> URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return base.appendingPathComponent("CloudSnapshots", isDirectory: true)
    }

    func load(_ route: CloudRoute) -> CachedCollection? {
        guard let data = try? Data(contentsOf: url(for: route)) else { return nil }
        // A cache that no longer decodes -- a model change, a truncated write,
        // a file somebody edited -- is not an error worth surfacing. It is a
        // cold start, which the client already handles.
        return try? JSONDecoder().decode(CachedCollection.self, from: data)
    }

    func store(_ collection: CachedCollection, for route: CloudRoute) {
        guard let data = try? JSONEncoder().encode(collection),
              data.count <= Self.maximumBytesPerRoute else { return }
        prepareDirectory()
        // `.atomic` so a crash or a kill mid-write leaves the previous cache
        // intact rather than a half-file that decodes as garbage.
        try? data.write(
            to: url(for: route),
            options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        )
    }

    func removeAll() {
        // Remove the whole directory rather than the routes this build knows
        // about: a route retired in a later version would otherwise leave its
        // file behind forever, and "clear the cache" has to mean all of it.
        try? FileManager.default.removeItem(at: directory)
    }

    private func url(for route: CloudRoute) -> URL {
        directory.appendingPathComponent("\(route.cacheKey).json", isDirectory: false)
    }

    private func prepareDirectory() {
        var directory = self.directory
        try? FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [
                .protectionKey: FileProtectionType.completeUntilFirstUserAuthentication,
            ]
        )
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try? directory.setResourceValues(resourceValues)
    }
}

/// The cache the tests use, and the one a build with no disk access can fall
/// back to.  Same contract, nothing durable.
final class MemorySnapshotCache: SnapshotCache, @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [String: CachedCollection] = [:]
    private(set) var clearCount = 0

    init() {}

    func load(_ route: CloudRoute) -> CachedCollection? {
        lock.lock()
        defer { lock.unlock() }
        return storage[route.cacheKey]
    }

    func store(_ collection: CachedCollection, for route: CloudRoute) {
        lock.lock()
        defer { lock.unlock() }
        storage[route.cacheKey] = collection
    }

    func removeAll() {
        lock.lock()
        defer { lock.unlock() }
        storage.removeAll()
        clearCount += 1
    }
}
