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
                Text("Open WatTracker on your computer and ask it for a pairing code.")
                    .font(.subheadline)
                    .foregroundStyle(Palette.muted)
            }

            Panel {
                VStack(alignment: .leading, spacing: 14) {
                    field(
                        title: "Name for this device",
                        note: "Shown in the device list on your computer."
                    ) {
                        TextField("iPad", text: $model.label)
                            .textFieldStyle(.roundedBorder)
                            .autocorrectionDisabled()
                            .submitLabel(.next)
                    }

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

    private var scanner: some View {
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
                    Text("Hold the code shown on your computer inside the frame.")
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                case .undetermined:
                    Text(
                        "WatTracker can read the pairing code with the camera so you do not "
                        + "have to type it."
                    )
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
                    Button("Use the camera") { Task { await model.requestCamera() } }
                        .buttonStyle(.bordered)
                        .tint(Palette.accent)
                case .denied:
                    Text(
                        "WatTracker does not have access to the camera. To scan instead of "
                        + "typing, open Settings > Privacy & Security > Camera and turn "
                        + "WatTracker on."
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
    var label: String = UIDevice.current.name
    var code: String = ""
    private(set) var isWorking = false
    private(set) var message: String?
    private(set) var cameraAccess: CameraAccess = .undetermined

    var canSubmit: Bool {
        !isWorking && !code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
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
    func scanned(_ value: String, gate: SessionGate) {
        guard !isWorking else { return }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let grouped = PairingCode.grouped(trimmed) else { return }
        code = grouped
        Task { await pair(gate: gate) }
    }

    func pair(gate: SessionGate) async {
        guard !isWorking else { return }
        let typed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !typed.isEmpty else { return }
        isWorking = true
        message = nil
        defer { isWorking = false }
        let name = label.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            try await gate.pair(code: typed, label: name.isEmpty ? nil : name)
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
