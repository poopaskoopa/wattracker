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

    private let makeSession: @Sendable () throws -> CloudSession

    init(makeSession: @escaping @Sendable () throws -> CloudSession = SessionGate.liveSession) {
        self.makeSession = makeSession
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

    /// Build the session and read where this device stands. Idempotent: the
    /// `.task` that calls it runs again whenever the gate's identity changes.
    func start() async {
        guard phase == .starting else { return }
        do {
            session = try makeSession()
        } catch {
            // Key creation failing is the one error that cannot be recovered
            // from in the app, so the reason is shown verbatim. It describes
            // the device, never a credential or a code.
            phase = .unusable(String(describing: error))
            return
        }
        await refresh()
    }

    /// Re-read the actor's state. Cheap -- no request -- and safe to call after
    /// anything that might have moved it.
    func refresh() async {
        guard let session else { return }
        phase = Self.phase(for: await session.deviceState)
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

    /// Revoke this device on the server, then wipe it locally. Both halves are
    /// `CloudSession.removeDevice`'s; this only makes the result visible.
    func removeDevice() async throws {
        guard let session else { throw GateFailure.noSession }
        do {
            try await session.removeDevice()
        } catch {
            await refresh()
            throw error
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
        guard let session else { return }
        await session.signOut()
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
        guard phase == .paired, let session else { return }
        _ = try? await session.readerContext()
        await refresh()
    }
}
