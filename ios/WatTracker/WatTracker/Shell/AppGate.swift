import SwiftUI

private struct CloudSessionKey: EnvironmentKey {
    static let defaultValue: CloudSession? = nil
}

extension EnvironmentValues {
    /// The app's one session, for screens that read the cloud.
    ///
    /// Optional because the environment has to have a default and there is no
    /// session before `AppGate` has built one; every screen that sees this is
    /// rendered underneath `.paired`, where it is never nil. A screen that
    /// finds it nil should render its empty state, not build a session of its
    /// own -- a second session means a second token, a second cache writer and
    /// a pairing one half of the app cannot see.
    var cloudSession: CloudSession? {
        get { self[CloudSessionKey.self] }
        set { self[CloudSessionKey.self] = newValue }
    }
}

/// The gate: which screen a launch lands on, and why it can change under you.
///
/// `WatTrackerApp` used to render `RootView` unconditionally, so a fresh
/// install arrived at a Dashboard with no credential behind it and no way to
/// get one. This branches instead, and the branch is on live state rather than
/// on a launch-time reading: pairing succeeds *while this view is on screen*,
/// and a revocation performed on the desktop shows up on the next probe rather
/// than the next launch.
struct AppGate: View {
    @State private var gate = SessionGate()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            switch gate.phase {
            case .starting:
                GateMessage(title: "WatTracker", detail: nil, showsProgress: true)
            case .unpaired:
                PairingScreen()
            case .paired:
                RootView()
                    .environment(\.cloudSession, gate.session)
            case .removed:
                RemovedScreen()
            case let .unusable(reason):
                GateMessage(
                    title: "This device cannot be paired",
                    detail: "WatTracker could not create a signing key on this device, so "
                        + "nothing it sends can be accepted.\n\n\(reason)",
                    showsProgress: false
                )
            }
        }
        .environment(gate)
        .background(Palette.bg.ignoresSafeArea())
        .task { await gate.start() }
        // Foregrounding is when a revocation performed while the app was in a
        // pocket becomes discoverable, and it is also the cheapest moment to
        // ask: the reader context has usually expired by then, so the probe
        // costs a refresh the next screen would have signed anyway.
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task { await gate.probe() }
        }
    }
}

/// The state this device is in after the rider -- or somebody at the desktop --
/// took its access away.
///
/// There is nothing to salvage and nothing to retry: `CloudSession.markRemoved`
/// has already destroyed the credential and the cache by the time this renders.
/// So the screen says what happened and offers the only action that exists,
/// which is to pair again.
private struct RemovedScreen: View {
    @Environment(SessionGate.self) private var gate

    var body: some View {
        GateMessage(
            title: "This device was removed",
            detail: "Its access to your training data has been revoked and everything it had "
                + "stored has been deleted. Pair again with a new code from the desktop app "
                + "to use WatTracker on this device.",
            showsProgress: false
        ) {
            Button("Pair again") { Task { await gate.startOver() } }
                .buttonStyle(.borderedProminent)
                .tint(Palette.accent)
                .foregroundStyle(Palette.onAccent)
        }
    }
}

/// A centred column of prose, for the two screens that are only prose.
///
/// The width cap is what makes this correct on both idioms without a special
/// case: ~440pt reads well across a landscape iPhone's 874pt and does not
/// stretch into a line-length nobody can track on a 1024pt portrait iPad, and
/// the `ScrollView` means a short landscape viewport with a large Dynamic Type
/// setting scrolls rather than clips.
private struct GateMessage<Actions: View>: View {
    let title: String
    let detail: String?
    let showsProgress: Bool
    private let actions: Actions

    init(
        title: String, detail: String?, showsProgress: Bool,
        @ViewBuilder actions: () -> Actions
    ) {
        self.title = title
        self.detail = detail
        self.showsProgress = showsProgress
        self.actions = actions()
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if showsProgress {
                    ProgressView().controlSize(.large).tint(Palette.accent)
                }
                Text(title)
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                if let detail {
                    Text(detail)
                        .font(.callout)
                        .foregroundStyle(Palette.muted)
                }
                actions
            }
            .multilineTextAlignment(.center)
            .frame(maxWidth: 440)
            .padding(24)
            .frame(maxWidth: .infinity)
        }
        .background(Palette.bg)
    }
}

extension GateMessage where Actions == EmptyView {
    init(title: String, detail: String?, showsProgress: Bool) {
        self.init(title: title, detail: detail, showsProgress: showsProgress) { EmptyView() }
    }
}

#Preview {
    AppGate().preferredColorScheme(.dark)
}
