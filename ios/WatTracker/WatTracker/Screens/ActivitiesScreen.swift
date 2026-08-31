import SwiftUI

/// Stub. The real activity list and detail are issue #162.
struct ActivitiesScreen: View {
    var body: some View {
        ScreenScaffold(
            title: "Activities",
            subtitle: "Every recorded ride"
        ) {
            StubPanel(
                note: "Will show the ride list with date, duration, distance, "
                    + "normalised power and TSS, and a detail view per ride.",
                issue: "stub - filled in by #162"
            )
        }
    }
}
