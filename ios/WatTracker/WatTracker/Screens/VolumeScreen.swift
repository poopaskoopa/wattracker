import SwiftUI

/// Stub. Calendar and volume are issue #163.
struct VolumeScreen: View {
    var body: some View {
        ScreenScaffold(
            title: "Volume",
            subtitle: "Hours and load over time"
        ) {
            StubPanel(
                note: "Will show weekly and monthly training volume as a "
                    + "Swift Charts bar chart, with a rolling load line.",
                issue: "stub - filled in by #163"
            )
        }
    }
}
