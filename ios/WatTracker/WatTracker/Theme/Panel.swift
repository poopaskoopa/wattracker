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
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(Palette.muted)
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
