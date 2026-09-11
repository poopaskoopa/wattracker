import Foundation
import Observation

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

    private(set) var phase: Phase = .starting

    /// The app's session, once there is one. Nil only in `starting` and
    /// `unusable`.
    private(set) var session: CloudSession?
    private(set) var localSession: LocalSession?
    private(set) var backend: Backend
    private(set) var lastSuccess: Date?

    private let makeSession: @Sendable () throws -> CloudSession
    private let makeLocalSession: @Sendable () throws -> LocalSession

    init(
        makeSession: @escaping @Sendable () throws -> CloudSession = SessionGate.liveSession,
        makeLocalSession: @escaping @Sendable () throws -> LocalSession = SessionGate.liveLocalSession
    ) {
        self.makeSession = makeSession
        self.makeLocalSession = makeLocalSession
        self.backend = .cloud
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
        if backend == .local, localPaired {
            // Keep the rider's explicit choice when its credential is present.
        } else if backend == .cloud, cloudPaired {
            // Keep the rider's explicit choice when its credential is present.
        } else if localPaired {
            backend = .local
        } else if cloudPaired {
            // A stale local preference must not hide a paired cloud session.
            backend = .cloud
        }
        if session == nil && !localPaired {
            phase = .unusable(String(describing: cloudError ?? GateFailure.noSession))
            return
        }
        await refresh()
    }

    /// Re-read the actor's state. Cheap -- no request -- and safe to call after
    /// anything that might have moved it.
    func refresh() async {
        guard let activeSession else { return }
        phase = Self.phase(for: await activeSession.deviceState)
        lastSuccess = await activeSession.lastSuccess
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
        do {
            try await localSession.pair(host: host, token: token, label: label)
        } catch {
            await refresh()
            throw error
        }
        backend = .local
        await refresh()
    }

    func selectBackend(_ backend: Backend) async {
        guard let candidate = backend == .cloud ? session : localSession,
              await candidate.deviceState == .paired else { return }
        self.backend = backend
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
        guard phase == .paired else { return }
        switch backend {
        case .cloud:
            guard let session else { return }
            _ = try? await session.readerContext()
        case .local:
            guard let localSession else { return }
            await localSession.probe()
        }
        await refresh()
    }
}
