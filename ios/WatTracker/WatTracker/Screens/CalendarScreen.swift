import SwiftUI

/// Stub. Calendar and volume are issue #163.
struct CalendarScreen: View {
    var body: some View {
        ScreenScaffold(
            title: "Calendar",
            subtitle: "Planned and completed, by week"
        ) {
            StubPanel(
                note: "Will show the training calendar: planned workouts "
                    + "against what was actually ridden.",
                issue: "stub - filled in by #163"
            )
        }
    }
}
