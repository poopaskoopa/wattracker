import Foundation
import Observation
import Network
import Security

protocol SessionPathMonitor: AnyObject {
    func start(onChange: @escaping @Sendable () -> Void)
    func cancel()
}

final class SystemSessionPathMonitor: SessionPathMonitor {
    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "com.wattracker.ios.network-path")
    private var hasStarted = false

    func start(onChange: @escaping @Sendable () -> Void) {
        guard !hasStarted else { return }
        hasStarted = true
        monitor.pathUpdateHandler = { _ in onChange() }
        monitor.start(queue: queue)
    }

    func cancel() {
        monitor.cancel()
    }
}

/// The one `CloudSession` in the app, and the observable answer to "which of
/// the three worlds is the rider in".
///
/// `CloudSession` is an actor and it is not observable: nothing about
/// `deviceState` changing wakes a SwiftUI view up. That is right for the
/// actor -- it is a lifecycle machine, not a view model -- and it is why this
/// exists. `SessionGate` is the `@MainActor` mirror the shell branches on, and
/// it is the only thing in the app allowed to build a session.
///
/// Owning the session here rather than letting each screen call a factory is
/// the fix for the concrete bug in #234: a screen with its own session cannot
/// see a pairing that happened on another screen, cannot see a revocation
/// another screen discovered, and holds a second copy of a coalescing refresh
/// whose whole value is that there is one of it. `AppGate` puts this session
/// into the environment; screens take it from there.
///
/// The phase is a snapshot, not a subscription. It moves when something asks
/// it to -- `start`, a pairing, a removal, `probe` on foreground -- because
/// the actor's own state only moves when a request is made, and polling an
/// actor that is not being asked anything would learn nothing.
@MainActor
@Observable
final class SessionGate {
    enum Backend: String, CaseIterable, Identifiable, Hashable {
        case cloud
        case local

        var id: String { rawValue }

        var title: String {
            switch self {
            case .cloud: return "Cloud"
            case .local: return "This desktop"
            }
        }
    }

    enum Selection: String, CaseIterable, Identifiable, Hashable {
        case automatic
        case cloud
        case local

        var id: String { rawValue }

        var title: String {
            switch self {
            case .automatic: return "Automatic"
            case .cloud: return "Cloud"
            case .local: return "This desktop"
            }
        }
    }

    enum Phase: Equatable {
        /// Before the signing key has been loaded. Distinct from `unpaired`
        /// because showing the pairing screen for the fraction of a second it
        /// takes to read the keychain would flash it at an already-paired
        /// rider on every cold start.
        case starting
        case unpaired
        case paired
        case removed
        /// No signing key could be made, so no request this app sends can ever
        /// be accepted. A Release build on a device without a Secure Enclave
        /// is the case that reaches here; there is nothing for the rider to
        /// retry, so the screen says so rather than offering pairing that
        /// cannot work.
        case unusable(String)
    }

    enum GateFailure: Error, CustomStringConvertible {
        case noSession

        var description: String { "This device has no signing key" }
    }

    protocol PreferenceStore {
        func loadBackendOverride() -> Backend?
        func saveBackendOverride(_ backend: Backend)
        func clearBackendOverride()
        func consumeLegacyBackend() -> Backend?
    }

    /// Reads a backend's state for `refresh`. Tests can suspend this seam to
    /// force a backend switch across the actor awaits.
    typealias StateReader = @Sendable (
        any ReadSession
    ) async -> (CloudSession.DeviceState, Date?)

    struct KeychainPreferenceStore: PreferenceStore, Sendable {
        private let service: String
        private let account: String

        init(
            service: String = "com.wattracker.ios.session",
            account: String = "backend-override-v2"
        ) {
            self.service = service
            self.account = account
        }

        func loadBackendOverride() -> Backend? {
            load(account: account)
        }

        func saveBackendOverride(_ backend: Backend) {
            var query = baseQuery(account: account)
            query[kSecValueData as String] = Data(backend.rawValue.utf8)
            query[kSecAttrAccessible as String] =
                kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            SecItemDelete(baseQuery(account: account) as CFDictionary)
            _ = SecItemAdd(query as CFDictionary, nil)
        }

        func clearBackendOverride() {
            SecItemDelete(baseQuery(account: account) as CFDictionary)
        }

        func consumeLegacyBackend() -> Backend? {
            defer { SecItemDelete(baseQuery(account: "selected-backend-v1") as CFDictionary) }
            return load(account: "selected-backend-v1")
        }

        private func load(account: String) -> Backend? {
            var query = baseQuery(account: account)
            query[kSecReturnData as String] = true
            query[kSecMatchLimit as String] = kSecMatchLimitOne
            var item: CFTypeRef?
            let status = withUnsafeMutablePointer(to: &item) {
                SecItemCopyMatching(query as CFDictionary, $0)
            }
            guard status == errSecSuccess, let data = item as? Data,
                  let rawValue = String(data: data, encoding: .utf8)
            else { return nil }
            return Backend(rawValue: rawValue)
        }

        private func baseQuery(account: String? = nil) -> [String: Any] {
            [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: account ?? self.account,
            ]
        }
    }

    private(set) var phase: Phase = .starting

    /// The app's session, once there is one. Nil only in `starting` and
    /// `unusable`.
    private(set) var session: CloudSession?
    private(set) var localSession: LocalSession?
    private(set) var backend: Backend
    private(set) var lastSuccess: Date?

    private let makeSession: @Sendable () throws -> CloudSession
    private let makeLocalSession: @Sendable () throws -> LocalSession
    private let stateReader: StateReader
    private let preferences: PreferenceStore
    private let pathMonitor: SessionPathMonitor
    private var manualOverride: Backend?
    /// The pairing screen's explicit choice is session-local. It is separate
    /// from the persisted Settings override, but automatic selection must
    /// still respect it until pairing leaves this flow or the rider chooses a
    /// different backend.
    private var temporaryOverride: Backend?
    private var selectionGeneration = 0
    private var automaticGeneration = 0
    private var automaticReevaluationTask: Task<Void, Never>?
    private var automaticReevaluationPending = false
    private var automaticReevaluationForcePending = false
    private var pathMonitorStarted = false

    var selection: Selection {
        manualOverride.map { $0 == .cloud ? .cloud : .local } ?? .automatic
    }

    init(
        makeSession: @escaping @Sendable () throws -> CloudSession = SessionGate.liveSession,
        makeLocalSession: @escaping @Sendable () throws -> LocalSession = SessionGate.liveLocalSession,
        preferences: PreferenceStore = KeychainPreferenceStore(),
        pathMonitor: SessionPathMonitor = SystemSessionPathMonitor(),
        stateReader: @escaping StateReader = { session in
            (await session.deviceState, await session.lastSuccess)
        }
    ) {
        self.makeSession = makeSession
        self.makeLocalSession = makeLocalSession
        self.stateReader = stateReader
        self.preferences = preferences
        self.pathMonitor = pathMonitor
        let storedBackend = preferences.loadBackendOverride()
        _ = preferences.consumeLegacyBackend()
        self.manualOverride = storedBackend
        self.backend = storedBackend ?? .cloud
    }

    deinit {
        pathMonitor.cancel()
    }

    /// The real session: an Enclave key where there is one, the keychain
    /// credential, and the on-disk cache.
    ///
    /// `nonisolated` because it is the default argument of an initialiser that
    /// can be written down from anywhere; the closure itself touches no state
    /// this class owns, and the keychain is only read when it is called.
    nonisolated static let liveSession: @Sendable () throws -> CloudSession = {
        let signer = try DeviceKeyStore.loadOrCreate()
        return CloudSession(
            client: CloudClient(baseURL: AppConfiguration.apiBaseURL, signer: signer),
            credentials: KeychainDeviceCredentialStore(),
            cache: FileSnapshotCache()
        )
    }

    nonisolated static let liveLocalSession: @Sendable () throws -> LocalSession = {
        LocalSession(
            credentials: KeychainLocalCredentialStore(),
            cache: FileSnapshotCache(
                directory: FileSnapshotCache.defaultDirectory()
                    .deletingLastPathComponent()
                    .appendingPathComponent("LocalSnapshots", isDirectory: true)
            )
        )
    }

    /// The session selected in Settings. Screens only receive this one value,
    /// so changing backends cannot leave one screen reading a second store.
    var activeSession: (any ReadSession)? {
        switch backend {
        case .cloud: return session
        case .local: return localSession
        }
    }

    /// Build the session and read where this device stands. Idempotent: the
    /// `.task` that calls it runs again whenever the gate's identity changes.
    func start() async {
        guard phase == .starting else { return }
        var cloudError: Error?
        do {
            session = try makeSession()
        } catch {
            cloudError = error
        }
        do {
            localSession = try makeLocalSession()
        } catch {
            // A local credential store is an optional second backend. The
            // cloud session remains the source of the launch error if both
            // stores are unusable.
        }

        let cloudPaired = await session?.deviceState == .paired
        let localPaired = await localSession?.deviceState == .paired
        if let manualOverride {
            backend = manualOverride
        } else {
            backend = cloudPaired ? .cloud : (localPaired ? .local : .cloud)
        }
        if session == nil && !localPaired {
            phase = .unusable(String(describing: cloudError ?? GateFailure.noSession))
            return
        }
        await refresh()
        Task { @MainActor [weak self] in await self?.automaticReevaluate() }
        guard !pathMonitorStarted else { return }
        pathMonitorStarted = true
        pathMonitor.start { [weak self] in
            Task { @MainActor in await self?.automaticReevaluate() }
        }
    }

    /// Prefer the already paired desktop when its endpoint answers. No host
    /// discovery occurs; LocalSession probes only its stored credential.
    func automaticReevaluate(force: Bool = false) async {
        automaticReevaluationPending = true
        automaticReevaluationForcePending = automaticReevaluationForcePending || force
        if automaticReevaluationTask == nil {
            automaticReevaluationTask = Task { @MainActor [weak self] in
                guard let self else { return }
                while self.automaticReevaluationPending {
                    self.automaticReevaluationPending = false
                    let force = self.automaticReevaluationForcePending
                    self.automaticReevaluationForcePending = false
                    await self.runAutomaticReevaluation(force: force)
                }
                self.automaticReevaluationTask = nil
            }
        }
        if let task = automaticReevaluationTask {
            await task.value
        }
    }

    private func runAutomaticReevaluation(force: Bool = false) async {
        if temporaryOverride != nil {
            await refresh()
            return
        }
        guard phase == .starting || phase == .paired || (force && phase == .unpaired)
        else { return }
        automaticGeneration += 1
        let automaticGeneration = self.automaticGeneration
        let selectionGeneration = self.selectionGeneration
        guard manualOverride == nil else {
            await probeSelectedBackend()
            guard automaticGeneration == self.automaticGeneration,
                  selectionGeneration == self.selectionGeneration
            else {
                await refresh()
                return
            }
            await refresh()
            return
        }
        let localPaired = await localSession?.deviceState == .paired
        let cloudPaired = await session?.deviceState == .paired
        let localReachable: Bool
        if localPaired, let localSession {
            localReachable = await localSession.probe()
        } else {
            localReachable = false
        }
        guard manualOverride == nil,
              automaticGeneration == self.automaticGeneration,
              selectionGeneration == self.selectionGeneration
        else {
            await refresh()
            return
        }
        if localReachable {
            backend = .local
        } else if cloudPaired {
            backend = .cloud
        } else if localPaired {
            // Keep the only paired backend selected when the desktop is
            // temporarily unavailable, so its cache remains usable.
            backend = .local
        }
        await refresh()
    }

    private func probeSelectedBackend() async {
        switch backend {
        case .cloud:
            if let session { _ = try? await session.readerContext() }
        case .local:
            if let localSession { _ = await localSession.probe() }
        }
    }

    /// Re-read the actor's state. Cheap -- no request -- and safe to call after
    /// anything that might have moved it.
    func refresh() async {
        let generation = selectionGeneration
        let selectedBackend = backend
        guard let activeSession else {
            guard generation == selectionGeneration, selectedBackend == backend else {
                return
            }
            phase = .unpaired
            lastSuccess = nil
            return
        }
        let (nextState, nextLastSuccess) = await stateReader(activeSession)
        let nextPhase = Self.phase(for: nextState)
        guard generation == selectionGeneration, selectedBackend == backend else {
            return
        }
        phase = nextPhase
        lastSuccess = nextLastSuccess
    }

    static func phase(for state: CloudSession.DeviceState) -> Phase {
        switch state {
        case .unpaired: return .unpaired
        case .paired: return .paired
        case .removed: return .removed
        }
    }

    /// Redeem a code. Throws what `CloudSession` threw; the caller renders it
    /// through `PairingFailureMessage` and never directly.
    func pair(code: String, label: String?) async throws {
        guard let session else { throw GateFailure.noSession }
        selectionGeneration += 1
        temporaryOverride = nil
        backend = .cloud
        do {
            try await session.pair(code: code, label: label)
        } catch {
            // A failed pairing can still have moved the session -- a lifecycle
            // bump lands before the throw -- so the phase is re-read either
            // way rather than assumed unchanged.
            await refresh()
            throw error
        }
        await refresh()
    }

    /// Pair the read-only desktop backend with a connector token. The token is
    /// tested before it is written to Keychain, and the local session owns the
    /// cookie it receives from the desktop.
    func pairLocal(host: String, token: String, label: String?) async throws {
        guard let localSession else { throw GateFailure.noSession }
        selectionGeneration += 1
        temporaryOverride = nil
        backend = .local
        do {
            try await localSession.pair(host: host, token: token, label: label)
        } catch {
            await refresh()
            throw error
        }
        await refresh()
    }

    /// Select the backend for the pairing flow. This choice is intentionally
    /// not persisted like the Settings selection, but it is still explicit for
    /// this gate and must not be replaced by automatic selection.
    func selectBackend(_ backend: Backend) async {
        let hasCandidate = backend == .cloud ? session != nil : localSession != nil
        guard hasCandidate else { return }
        selectionGeneration += 1
        temporaryOverride = backend
        self.backend = backend
        await refresh()
    }

    func select(_ selection: Selection) async {
        switch selection {
        case .automatic:
            selectionGeneration += 1
            manualOverride = nil
            temporaryOverride = nil
            preferences.clearBackendOverride()
            await automaticReevaluate(force: true)
        case .cloud, .local:
            let backend: Backend = selection == .cloud ? .cloud : .local
            temporaryOverride = nil
            await selectBackendOverride(backend)
        }
    }

    private func selectBackendOverride(_ backend: Backend) async {
        let candidate: (any ReadSession)? = backend == .cloud ? session : localSession
        guard let candidate else { return }
        selectionGeneration += 1
        manualOverride = backend
        self.backend = backend
        preferences.saveBackendOverride(backend)
        await refresh()
    }

    func cloudDevices() async throws -> [CloudDevice] {
        guard let session else { throw GateFailure.noSession }
        return try await session.devices()
    }

    func localBaseURL() async -> String? {
        await localSession?.pairingAddress
    }

    /// Revoke this device on the server, then wipe it locally. Both halves are
    /// `CloudSession.removeDevice`'s; this only makes the result visible.
    func removeDevice() async throws {
        selectionGeneration += 1
        temporaryOverride = nil
        switch backend {
        case .cloud:
            guard let session else { throw GateFailure.noSession }
            do {
                try await session.removeDevice()
            } catch {
                await refresh()
                throw error
            }
        case .local:
            guard let localSession else { throw GateFailure.noSession }
            await localSession.signOut()
            if await session?.deviceState == .paired {
                backend = .cloud
            }
        }
        await refresh()
    }

    /// Leave the removed state and offer pairing again.
    ///
    /// The credential and cache are already gone by the time `removed` is
    /// observable -- `markRemoved` wipes them -- so this is only the state
    /// change, and `signOut` is used rather than a direct assignment so the
    /// actor stays the one place that decides what "not paired" means.
    func startOver() async {
        selectionGeneration += 1
        temporaryOverride = nil
        switch backend {
        case .cloud:
            if let session { await session.signOut() }
        case .local:
            if let localSession { await localSession.signOut() }
        }
        await refresh()
    }

    /// Ask the server a question only a live credential can answer, then
    /// re-read the phase.
    ///
    /// A signed context refresh is the probe rather than, say, the device list,
    /// because it is the *only* request `CloudSession` counts toward removal:
    /// `refusal` reaches `markRemoved` from `performRefresh` and from nowhere
    /// else. Reading a collection or listing devices can fail all day without
    /// ever concluding anything, by design -- only proving possession of the
    /// device key is allowed to decide the key no longer works.
    ///
    /// Errors are swallowed: every one of them is either transient or already
    /// recorded in the phase this then reads. Two refusals are needed, with the
    /// backoff between them, so a single foregrounding cannot unpair anything.
    func probe() async {
        if let session, await session.deviceState == .paired {
            _ = try? await session.readerContext()
        }
        await automaticReevaluate()
    }
}
