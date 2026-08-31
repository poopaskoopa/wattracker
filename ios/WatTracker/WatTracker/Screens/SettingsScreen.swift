import SwiftUI

/// Stub, plus the one thing in this app that is not a stub.
///
/// Pairing and account settings proper are issue #160. What is real here is
/// the entry point to `FTPScreen` -- issue #171's walking skeleton, which
/// carries a live FTP from the rider's desktop database to the device across
/// the actual pairing, signing and read planes.
///
/// It is kept, and kept runnable, because it is the only on-device evidence
/// that the canonical-request signing and Secure Enclave key paths work end to
/// end; #171's own write-up notes that the deployment and subject-binding
/// paths are covered by the Python suite rather than by that run, so the one
/// live proof should not be replaced by a stub. It is labelled as a debug tool
/// so nobody mistakes it for a shipping screen, and #161 can retire it once
/// the real Dashboard reads the same data.
struct SettingsScreen: View {
    @State private var showingFTPRoundTrip = false

    var body: some View {
        ScreenScaffold(
            title: "Settings",
            subtitle: "Pairing, units and account"
        ) {
            StubPanel(
                note: "Will show the paired desktop, the signing key's "
                    + "provenance, units, and how to unpair this device.",
                issue: "stub - filled in by #160"
            )

            Panel {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Debug: FTP round-trip")
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(Palette.textBright)
                    Text(
                        "Issue #171's walking skeleton. Pairs with a code, "
                        + "signs a refresh and reads one real FTP from the "
                        + "desktop database. Needs a local server; see "
                        + "ios/README.md. Not a shipping screen."
                    )
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
                    Button("Run FTP round-trip") { showingFTPRoundTrip = true }
                        .buttonStyle(.bordered)
                        .tint(Palette.accent)
                }
            }
        }
        // Presented rather than pushed so it behaves identically on the rail
        // and inside the iPad split view's detail stack, and so the debug
        // screen keeps the full-bleed layout it was written for.
        .fullScreenCover(isPresented: $showingFTPRoundTrip) {
            ZStack(alignment: .topTrailing) {
                FTPScreen()
                Button("Close") { showingFTPRoundTrip = false }
                    .buttonStyle(.bordered)
                    .tint(Palette.accent)
                    .padding(16)
            }
            .preferredColorScheme(.dark)
        }
    }
}
