import Observation
import SwiftUI

/// The panel: a bordered container on the app background.
///
/// This exists so the five screen stubs cannot drift into five slightly
/// different ideas of what a container looks like before the real screens
/// (#161 onward) are written. It is the SwiftUI equivalent of the web app's
/// `.panel` rule, and it is the only place the corner radius and hairline are
/// specified.
struct Panel<Content: View>: View {
    private let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(Palette.panel, in: .rect(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(Palette.surfaceBorder, lineWidth: 1)
            }
    }
}

/// The common frame every screen sits in: a title, then content, on `bg`.
///
/// The title is rendered here rather than with `.navigationTitle` because only
/// the iPad path has a navigation bar to put it in. On the iPhone rail path
/// there is no bar at all -- deliberately, since a navigation bar would cost
/// another ~44pt of the scarce vertical axis for a string the rail already
/// shows as the selected item. Rendering it in the content keeps both idioms
/// showing the same thing without a bar.
struct ScreenScaffold<Content: View>: View {
    @Environment(SessionGate.self) private var gate
    let title: String
    let subtitle: String
    private let content: Content

    init(title: String, subtitle: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.content = content()
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(Palette.textBright)
                    HStack(alignment: .firstTextBaseline) {
                        Text(subtitle)
                            .font(.subheadline)
                            .foregroundStyle(Palette.muted)
                        Spacer(minLength: 8)
                        if gate.phase == .paired {
                            Label(gate.backend.title, systemImage: "arrow.triangle.2.circlepath")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(Palette.muted)
                        }
                    }
                }
                content
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Palette.bg)
    }
}

/// A short line of placeholder text inside a panel.
///
/// Every screen in this shell is a stub. They say what they will hold and
/// which issue fills them in, and they show no fake numbers and no fake
/// charts: a placeholder that looks like data is a screenshot waiting to be
/// mistaken for a working feature.
struct StubPanel: View {
    let note: String
    let issue: String

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 8) {
                Text(note)
                    .font(.callout)
                    .foregroundStyle(Palette.text)
                Text(issue)
                    .font(.caption.monospaced())
                    .foregroundStyle(Palette.muted)
            }
        }
    }
}

/// Whether a read has been in flight long enough to be the cloud waking up.
///
/// The cloud read app scales to zero, so the first read after an idle spell
/// can take most of a minute. Past `threshold` a screen says so, calmly,
/// rather than leaving a spinner that looks stuck -- and keeps whatever cached
/// data it already has on screen underneath. Only for a session that can be
/// asleep (`ReadSession.mayBeWaking`); the desktop is never "waking".
@MainActor
@Observable
final class CloudWakeNotice {
    static let threshold: Duration = .seconds(5)

    private(set) var isShowing = false
    @ObservationIgnored private var active = 0
    @ObservationIgnored private var timer: Task<Void, Never>?

    /// Run `read`, showing the notice if it is still running after
    /// `threshold`. Overlapping reads keep it up until the last one ends.
    func during<T>(
        _ session: any ReadSession, _ read: () async throws -> T
    ) async rethrows -> T {
        guard session.mayBeWaking else { return try await read() }
        active += 1
        if timer == nil {
            timer = Task { [weak self] in
                try? await Task.sleep(for: Self.threshold)
                guard !Task.isCancelled, let self, self.active > 0 else { return }
                self.isShowing = true
            }
        }
        defer {
            active -= 1
            if active == 0 {
                timer?.cancel()
                timer = nil
                isShowing = false
            }
        }
        return try await read()
    }
}

/// What a screen shows while `CloudWakeNotice` is up.
struct CloudWakingPanel: View {
    /// Whether cached data is on screen below this.
    let showingCache: Bool

    var body: some View {
        Panel {
            HStack(alignment: .top, spacing: 12) {
                ProgressView()
                    .tint(Palette.accent)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Waking up the cloud…")
                        .font(.headline)
                        .foregroundStyle(Palette.textBright)
                    Text(
                        showingCache
                            ? "It sleeps when idle and can take up to a minute. "
                                + "Showing your last sync meanwhile."
                            : "It sleeps when idle and can take up to a minute."
                    )
                    .font(.callout)
                    .foregroundStyle(Palette.muted)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}
