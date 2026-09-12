import SwiftUI
import UIKit

/// How a rider gets a credential onto this device: type twelve symbols, or
/// point the camera at the QR the desktop is showing.
///
/// ## Why both, and why typing is the one that must never break
///
/// Scanning is the fast path and the one that cannot be mistyped, but it is
/// also the one with a permission prompt in front of it, no camera at all on
/// the simulator, and a rider who may have said no months ago for a different
/// reason. Typing has none of those failure modes. So the code field is always
/// present and always enabled, and the camera is an accelerator beside it
/// rather than a step in a flow -- every camera state this screen can be in
/// still leaves a working way to pair.
///
/// ## The layout, on two idioms and three shapes
///
/// The two halves sit side by side when there is room and stack when there is
/// not, and the threshold is a measured width rather than a size class. A size
/// class would get this wrong in both directions: a landscape iPhone is
/// ~874pt wide and reports *compact*, and an iPad in Slide Over is ~320pt wide
/// and reports compact as well. 700pt separates the layouts that actually have
/// room for two columns from the ones that do not, which puts a landscape
/// phone and a portrait iPad on the same side of the line -- the portrait iPad
/// being the shape #158 makes a standing requirement to get right.
struct PairingScreen: View {
    @Environment(SessionGate.self) private var gate
    @State private var model = PairingModel()

    var body: some View {
        GeometryReader { proxy in
            ScrollView {
                Group {
                    if proxy.size.width >= 700 {
                        HStack(alignment: .top, spacing: 16) {
                            form.frame(maxWidth: .infinity, alignment: .leading)
                            scanner.frame(maxWidth: .infinity)
                        }
                    } else {
                        VStack(alignment: .leading, spacing: 16) {
                            form
                            scanner
                        }
                    }
                }
                .padding(16)
                .frame(maxWidth: 900, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .background(Palette.bg)
        }
        .task { model.refreshCameraAccess() }
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Pair this device")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(Palette.textBright)
                Text(
                    model.backend == .cloud
                        ? "Open wattracker on your computer and ask it for a pairing code."
                        : "Enter the HTTPS address and connector token from your desktop server."
                )
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
            }

            Panel {
                VStack(alignment: .leading, spacing: 14) {
                    Picker("Data source", selection: $model.backend) {
                        ForEach(SessionGate.Backend.allCases) { backend in
                            Text(backend.title).tag(backend)
                        }
                    }
                    .pickerStyle(.segmented)

                    if model.backend == .cloud {
                        field(
                            title: "Name for this device",
                            note: "Shown in the device list on your computer."
                        ) {
                            TextField("iPad", text: $model.label)
                                .textFieldStyle(.roundedBorder)
                                .autocorrectionDisabled()
                                .submitLabel(.next)
                        }
                    }

                    if model.backend == .cloud {
                        field(title: "Pairing code", note: nil) {
                            TextField("XXXX-XXXX-XXXX", text: $model.code)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(.title3, design: .monospaced))
                                .textInputAutocapitalization(.characters)
                                .autocorrectionDisabled()
                                .submitLabel(.go)
                                .onSubmit { pair() }
                                .accessibilityIdentifier("pairing-code")
                        }
                    } else {
                        field(
                            title: "Desktop HTTPS address",
                            note: "Use the TLS terminator address in front of the desktop server."
                        ) {
                            TextField("https://desktop.example", text: $model.localHost)
                                .textFieldStyle(.roundedBorder)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .keyboardType(.URL)
                                .accessibilityIdentifier("local-host")
                        }
                        field(
                            title: "Connector token",
                            note: "Create a device token in the desktop web settings."
                        ) {
                            SecureField("Token", text: $model.localToken)
                                .textFieldStyle(.roundedBorder)
                                .autocorrectionDisabled()
                                .textInputAutocapitalization(.never)
                                .submitLabel(.go)
                                .onSubmit { pair() }
                                .accessibilityIdentifier("local-token")
                        }
                    }

                    HStack(spacing: 12) {
                        Button("Pair") { pair() }
                            .buttonStyle(.borderedProminent)
                            .tint(Palette.accent)
                            .foregroundStyle(Palette.onAccent)
                            .disabled(!model.canSubmit)
                        if model.isWorking {
                            ProgressView().tint(Palette.accent)
                        }
                    }

                    if let message = model.message {
                        Text(message)
                            .font(.callout)
                            .foregroundStyle(Palette.alert)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("pairing-message")
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var scanner: some View {
        if model.backend == .cloud {
            Panel {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Or scan the QR code")
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(Palette.textBright)
                    switch model.cameraAccess {
                    case .allowed:
                        QRCodeScannerView { scanned in
                            model.scanned(scanned, gate: gate)
                        }
                        // 4:3 matches the capture aspect, so the preview fills the
                        // frame without the crop `resizeAspectFill` would otherwise
                        // take out of the sides.
                        .aspectRatio(4 / 3, contentMode: .fit)
                        .frame(maxWidth: .infinity)
                        .clipShape(.rect(cornerRadius: 10))
                        Text(
                            model.scanHint
                                ?? "Hold the code shown on your computer inside the frame."
                        )
                        .font(.caption)
                        .foregroundStyle(model.scanHint == nil ? Palette.muted : Palette.accent)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("pairing-scan-hint")
                    case .undetermined:
                        Text(
                            "wattracker can read the pairing code with the camera so you do not "
                            + "have to type it."
                        )
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                        Button("Use the camera") { Task { await model.requestCamera() } }
                            .buttonStyle(.bordered)
                            .tint(Palette.accent)
                    case .denied:
                        Text(
                            "wattracker does not have access to the camera. To scan instead of "
                            + "typing, open Settings > Privacy & Security > Camera and turn "
                            + "wattracker on."
                        )
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                        Button("Open Settings") { model.openSettings() }
                            .buttonStyle(.bordered)
                            .tint(Palette.accent)
                    case .unavailable:
                        Text("This device has no camera available, so type the code instead.")
                            .font(.caption)
                            .foregroundStyle(Palette.muted)
                    }
                }
            }
        } else {
            Panel {
                Text(
                    "Local pairing uses the HTTPS address and connector token from the "
                        + "desktop web settings. The token is stored only in this device's Keychain."
                )
                .font(.caption)
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private func field<Content: View>(
        title: String, note: String?, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Palette.muted)
            content()
            if let note {
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(Palette.muted)
            }
        }
    }

    private func pair() {
        Task { await model.pair(gate: gate) }
    }
}

@MainActor
@Observable
final class PairingModel {
    var backend: SessionGate.Backend = .cloud
    var label: String = UIDevice.current.name
    var code: String = ""
    var localHost: String = ""
    var localToken: String = ""
    private(set) var isWorking = false
    private(set) var message: String?
    /// What the camera is seeing, when that is not a pairing code.
    ///
    /// Kept apart from `message` deliberately. `message` is what came back
    /// from an attempt; this is a statement about a barcode that was never
    /// sent anywhere, so it can say exactly what happened without being an
    /// oracle for anything. Merging the two would put a local observation in
    /// the place the rider has learned to read server verdicts.
    private(set) var scanHint: String?
    private(set) var cameraAccess: CameraAccess = .undetermined

    var canSubmit: Bool {
        guard !isWorking else { return false }
        switch backend {
        case .cloud:
            return !code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case .local:
            return !localHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                && !localToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    func refreshCameraAccess() {
        cameraAccess = CameraAccess.current
    }

    func requestCamera() async {
        cameraAccess = await CameraAccess.request()
    }

    func openSettings() {
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
        UIApplication.shared.open(url)
    }

    /// A QR arrived. Fill the field with it and redeem it without a second tap.
    ///
    /// The shape check is on scans and not on typed input, and the asymmetry is
    /// deliberate. A scan is unattended -- the camera sees whatever is pointed
    /// at it, including a Wi-Fi barcode on the back of a router -- so something
    /// that cannot be a pairing code is ignored rather than spent as an
    /// attempt. Typed input is sent exactly as the rider wrote it: a
    /// client-side rule that drifted from the server's would refuse a code the
    /// server would have taken, and the rider would have no way to tell that
    /// apart from a rejected one.
    /// A barcode that cannot be a pairing code is not silently discarded:
    /// with no feedback, a rider pointing the camera at the wrong thing sees
    /// a live preview and nothing happening, which looks like a broken
    /// scanner rather than a wrong barcode. Saying so costs nothing here --
    /// this code never reached the server, so there is no outcome to leak.
    func scanned(_ value: String, gate: SessionGate) {
        guard backend == .cloud, !isWorking else { return }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let grouped = PairingCode.grouped(trimmed) else {
            scanHint = "That barcode is not a wattracker pairing code. "
                + "Point the camera at the code shown on your computer."
            return
        }
        scanHint = nil
        code = grouped
        Task { await pair(gate: gate) }
    }

    func pair(gate: SessionGate) async {
        guard !isWorking else { return }
        let typed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        let host = localHost.trimmingCharacters(in: .whitespacesAndNewlines)
        let token = localToken.trimmingCharacters(in: .whitespacesAndNewlines)
        switch backend {
        case .cloud:
            guard !typed.isEmpty else { return }
        case .local:
            guard !host.isEmpty, !token.isEmpty else { return }
        }
        isWorking = true
        message = nil
        defer { isWorking = false }
        let name = label.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            switch backend {
            case .cloud:
                try await gate.pair(code: typed, label: name.isEmpty ? nil : name)
            case .local:
                try await gate.pairLocal(
                    host: host, token: token, label: name.isEmpty ? nil : name
                )
                localToken = ""
            }
            // Nothing else to do: the gate's phase is already `.paired` and
            // `AppGate` has replaced this screen with the shell.
        } catch {
            // Never `error` itself and never `CloudSession.Failure.description`
            // -- see `PairingFailureMessage` for why the mapping is the whole
            // point.
            message = PairingFailureMessage.text(for: error)
        }
    }
}

#Preview {
    PairingScreen()
        .environment(SessionGate())
        .preferredColorScheme(.dark)
}
