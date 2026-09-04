import SwiftUI

/// The pairing this device is running on, and how to end it.
///
/// Three questions, which are the ones a rider actually has about a paired
/// device: what am I paired to, is it still syncing, and how do I get this
/// device out of my account. The device list the server returns answers all
/// three -- it is scoped to one account, so the devices in it *are* the
/// account, and `last_seen_at` on this device's own row is the server's record
/// of when it last accepted a signed request from here.
///
/// Removal is `CloudSession.removeDevice`: the server revoke first, the local
/// wipe second, and that order is not cosmetic. A local wipe that ran first
/// would leave a credential the server still honours and no key left to revoke
/// it with.
///
/// The FTP round-trip below stays. It is issue #171's walking skeleton and the
/// only on-device evidence that the canonical-request signing and Secure
/// Enclave paths work end to end; #161 retires it once the real Dashboard
/// reads the same data through the same planes, and not before.
struct SettingsScreen: View {
    @Environment(SessionGate.self) private var gate
    @Environment(\.cloudSession) private var session
    @State private var model = SettingsModel()
    @State private var showingFTPRoundTrip = false
    @State private var confirmingRemoval = false

    var body: some View {
        ScreenScaffold(
            title: "Settings",
            subtitle: "Pairing, units and account"
        ) {
            pairingPanels
            removalPanel
            debugPanel
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
        .task { await model.load(session: session, gate: gate) }
    }

    @ViewBuilder
    private var pairingPanels: some View {
        switch model.state {
        case .loading:
            Panel {
                HStack(spacing: 10) {
                    ProgressView().tint(Palette.accent)
                    Text("Reading the device list")
                        .font(.callout)
                        .foregroundStyle(Palette.muted)
                }
            }
        case let .failed(message):
            Panel {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Could not read the device list")
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(Palette.textBright)
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(Palette.alert)
                    Button("Try again") {
                        Task { await model.reload(session: session, gate: gate) }
                    }
                    .buttonStyle(.bordered)
                    .tint(Palette.accent)
                }
            }
        case let .loaded(devices):
            thisDevicePanel(devices.first { $0.isSelf })
            otherDevicesPanel(devices.filter { !$0.isSelf })
        }
    }

    @ViewBuilder
    private func thisDevicePanel(_ device: CloudDevice?) -> some View {
        Panel {
            VStack(alignment: .leading, spacing: 8) {
                Text("This device")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                row("Name", device?.label ?? "Unnamed device")
                row("Paired with", AppConfiguration.apiBaseURL.host ?? "—")
                row("Last synced", SettingsModel.lastSeen(device?.lastSeenAt))
                if let credentialID = device?.credentialID {
                    row("Credential", credentialID, monospaced: true)
                }
            }
        }
    }

    @ViewBuilder
    private func otherDevicesPanel(_ devices: [CloudDevice]) -> some View {
        Panel {
            VStack(alignment: .leading, spacing: 8) {
                Text("Other devices on this account")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                if devices.isEmpty {
                    Text("None. This is the only device paired to your account.")
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                } else {
                    ForEach(devices, id: \.credentialID) { device in
                        row(
                            device.label ?? "Unnamed device",
                            SettingsModel.lastSeen(device.lastSeenAt)
                        )
                    }
                }
            }
        }
    }

    private var removalPanel: some View {
        Panel {
            VStack(alignment: .leading, spacing: 10) {
                Text("Remove this device")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                Text(
                    "Revokes this device's access on the server and deletes everything it has "
                    + "stored. You will need a new pairing code to use WatTracker here again."
                )
                .font(.caption)
                .foregroundStyle(Palette.muted)
                Button("Remove this device", role: .destructive) { confirmingRemoval = true }
                    .buttonStyle(.bordered)
                    .tint(Palette.alert)
                    .disabled(model.isRemoving)
                if model.isRemoving {
                    ProgressView().tint(Palette.accent)
                }
                if let failure = model.removalFailure {
                    Text(failure)
                        .font(.caption)
                        .foregroundStyle(Palette.alert)
                }
            }
        }
        .confirmationDialog(
            "Remove this device?",
            isPresented: $confirmingRemoval,
            titleVisibility: .visible
        ) {
            Button("Remove", role: .destructive) {
                Task { await model.remove(gate: gate) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This cannot be undone. Pairing again needs a new code from the desktop app.")
        }
    }

    private var debugPanel: some View {
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

    private func row(_ label: String, _ value: String, monospaced: Bool = false) -> some View {
        // Not a `Grid`: the value column is the one that has to wrap on a
        // narrow iPad window, and a grid would either clip it or force the
        // label column wider than it needs to be.
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .font(.caption)
                .foregroundStyle(Palette.muted)
                .frame(width: 96, alignment: .leading)
            Text(value)
                .font(monospaced ? .caption.monospaced() : .callout)
                .foregroundStyle(Palette.text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

@MainActor
@Observable
final class SettingsModel {
    enum State {
        case loading
        case loaded([CloudDevice])
        case failed(String)
    }

    private(set) var state: State = .loading
    private(set) var isRemoving = false
    private(set) var removalFailure: String?

    private var hasLoaded = false

    func load(session: CloudSession?, gate: SessionGate) async {
        guard !hasLoaded else { return }
        hasLoaded = true
        await reload(session: session, gate: gate)
    }

    /// Probe first, then list.
    ///
    /// The probe is what makes "revoking from the desktop shows up here"
    /// true: only a signed context refresh counts toward `CloudSession`'s
    /// two-strike removal, so listing devices could fail forever without the
    /// app ever concluding it had been revoked. It costs one refresh, which is
    /// a request this screen's own signed call would have needed anyway once
    /// the context expired.
    func reload(session: CloudSession?, gate: SessionGate) async {
        state = .loading
        await gate.probe()
        guard let session else {
            state = .failed(SessionGate.GateFailure.noSession.description)
            return
        }
        do {
            state = .loaded(try await session.devices())
        } catch {
            // No indistinguishability constraint here: this is an authenticated
            // request by an already-paired device, so its failure says nothing
            // about a secret anybody is guessing, and the specific reason is
            // what makes it diagnosable.
            state = .failed(Self.describe(error))
            await gate.refresh()
        }
    }

    func remove(gate: SessionGate) async {
        guard !isRemoving else { return }
        isRemoving = true
        removalFailure = nil
        defer { isRemoving = false }
        do {
            try await gate.removeDevice()
            // Nothing to reset: the gate is `.unpaired` now and this screen
            // has been replaced by the pairing screen.
        } catch {
            removalFailure = Self.describe(error)
        }
    }

    static func describe(_ error: Error) -> String {
        if let failure = error as? CloudSession.Failure { return failure.description }
        if let failure = error as? SessionGate.GateFailure { return failure.description }
        return String(describing: error)
    }

    /// `last_seen_at` as a phrase, or an em dash when the server has no record
    /// of this device having read anything yet.
    static func lastSeen(_ timestamp: Double?, now: Date = Date()) -> String {
        guard let timestamp else { return "Never" }
        let date = Date(timeIntervalSince1970: timestamp)
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return formatter.localizedString(for: date, relativeTo: now)
    }
}
