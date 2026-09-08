import Foundation
import Observation

/// What the activities list is allowed to show, and the reads that decide it.
///
/// The screen used to hold `snapshot` + `errorMessage` in `@State` and funnel
/// every failure into the message. That cannot express "stop showing this
/// rider's rides": a revoke observed during a pull-to-refresh wiped the
/// credential and the cache inside the session, but the already-rendered
/// snapshot stayed in the view, so the revoked rider's full ride list kept
/// rendering with an error line above it until the app was backgrounded. The
/// terminal states clear the data here, the way `DashboardModel` does.
@MainActor
@Observable
final class ActivitiesModel {
    enum Availability: Equatable {
        case available
        case unpaired
        case removed
    }

    private(set) var snapshot: CloudSnapshot?
    private(set) var availability: Availability = .available
    private(set) var errorMessage: String?
    private(set) var isLoading = false
    var selectedID: String?

    var rides: [RideSummary] {
        (snapshot?.items ?? []).compactMap(RideSummary.init(item:)).sorted {
            if $0.startedAt != $1.startedAt { return $0.startedAt > $1.startedAt }
            return $0.id > $1.id
        }
    }

    /// Takes the session rather than building one.
    ///
    /// This screen used to build its own when none was passed in. That
    /// predates the gate: two sessions over one keychain credential means a
    /// sign-out or a revoke performed in Settings leaves this screen holding
    /// one that still believes it is paired, listing a signed-out rider's
    /// rides. The signing-key failure the old initialiser reported as
    /// `startupError` is the gate's to report now -- it never gets as far as
    /// showing this screen.
    func refresh(session: CloudSession?) async {
        // No session means the gate has not produced one: no credential yet,
        // or the signing key could not be read. Neither is a ride list.
        guard let session else {
            clear(.unpaired, message: CloudSession.Failure.notPaired.description)
            return
        }

        switch await session.deviceState {
        case .unpaired:
            clear(.unpaired, message: CloudSession.Failure.notPaired.description)
            return
        case .removed:
            clear(.removed, message: CloudSession.Failure.deviceRemoved.description)
            return
        case .paired:
            break
        }

        // First paint from cache before the network, the way the Dashboard
        // does it. In the view's `init` previously; the injected session is
        // not readable there.
        if snapshot == nil { snapshot = session.cached(.activities) }
        isLoading = true
        defer { isLoading = false }
        do {
            snapshot = try await session.load(.activities)
            availability = .available
            errorMessage = nil
            if selectedID == nil { selectedID = rides.first?.id }
        } catch let failure as CloudSession.Failure {
            switch failure {
            case .notPaired:
                clear(.unpaired, message: failure.description)
            case .deviceRemoved:
                clear(.removed, message: failure.description)
            default:
                // A cached, usable list stays visible when the refresh fails
                // for an ordinary network reason -- the session normally
                // returns it as `.cache` anyway. Revocation and unpairing are
                // handled above so they always clear the view.
                errorMessage = failure.description
            }
        } catch {
            errorMessage = String(describing: error)
        }
    }

    /// Leave nothing of the previous rider's rides behind.
    private func clear(_ availability: Availability, message: String) {
        self.availability = availability
        snapshot = nil
        selectedID = nil
        errorMessage = message
    }
}

struct RideSummary: Identifiable {
    let id: String
    let activityID: Int
    let summary: ActivitySummary
    let startedAt: String

    init?(item: CloudItem) {
        guard !item.deleted, case let .activity(summary) = item.payload else { return nil }
        let numericID = summary.id.map(Int.init)
            ?? Int(item.id.split(separator: "-").last ?? "")
        guard let numericID else { return nil }
        id = item.id
        activityID = numericID
        self.summary = summary
        startedAt = summary.startTime ?? ""
    }
}
