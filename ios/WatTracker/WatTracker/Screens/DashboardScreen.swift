import SwiftUI

/// Stub. The real dashboard is issue #161.
struct DashboardScreen: View {
    var body: some View {
        ScreenScaffold(
            title: "Dashboard",
            subtitle: "Today's form, fitness and fatigue"
        ) {
            StubPanel(
                note: "Will show CTL, ATL and TSB, the recent-rides strip and "
                    + "the current FTP, read from the paired desktop database.",
                issue: "stub - filled in by #161"
            )
        }
    }
}
