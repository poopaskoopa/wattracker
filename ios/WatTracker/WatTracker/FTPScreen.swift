import SwiftUI

/// The whole app: one number, in landscape.
///
/// There is deliberately no navigation, no tab bar, no chart and no cache.
/// This screen exists to prove that a number in the rider's desktop database
/// can reach a phone through the real pairing, signing and read planes. Every
/// screen in the epic proper is a separate issue.
struct FTPScreen: View {
    @State private var model = FTPModel()

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            // A wide, short viewport is the primary constraint, so the layout
            // is a single centred column with generous horizontal room rather
            // than anything that stacks vertically.
            VStack(spacing: 12) {
                Spacer(minLength: 0)
                content
                Spacer(minLength: 0)
                footer
            }
            .padding(.horizontal, 48)
            .padding(.vertical, 16)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .task { await model.start() }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .working(let step):
            VStack(spacing: 16) {
                ProgressView().controlSize(.large).tint(.white)
                Text(step)
                    .font(.title3)
                    .foregroundStyle(.white.opacity(0.7))
            }
        case .needsCode:
            VStack(spacing: 16) {
                Text("Pairing code")
                    .font(.title3)
                    .foregroundStyle(.white.opacity(0.7))
                TextField("XXXX-XXXX-XXXX", text: $model.typedCode)
                    .textFieldStyle(.roundedBorder)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .font(.system(.title, design: .monospaced))
                    .frame(maxWidth: 420)
                Button("Pair") { Task { await model.pairWithTypedCode() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.typedCode.isEmpty)
            }
        case .ftp(let watts):
            VStack(spacing: 4) {
                Text(FTPModel.format(watts))
                    .font(.system(size: 132, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .minimumScaleFactor(0.4)
                    .lineLimit(1)
                    .foregroundStyle(.white)
                    .accessibilityIdentifier("ftp-watts")
                Text("WATTS FTP")
                    .font(.title3.weight(.semibold))
                    .tracking(4)
                    .foregroundStyle(.white.opacity(0.55))
            }
        case .failed(let message):
            VStack(spacing: 12) {
                Text("Could not read the profile")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(.white)
                Text(message)
                    .font(.callout)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.orange)
            }
        }
    }

    private var footer: some View {
        // The provenance line. A build using a keychain key instead of the
        // Secure Enclave says so on screen, so a screenshot of this app can
        // never be mistaken for evidence that hardware keys work.
        Text(model.provenance)
            .font(.caption.monospaced())
            .foregroundStyle(.white.opacity(0.45))
            .multilineTextAlignment(.center)
    }
}

@MainActor
@Observable
final class FTPModel {
    enum State {
        case working(String)
        case needsCode
        case ftp(Double)
        case failed(String)
    }

    private(set) var state: State = .working("Starting")
    private(set) var provenance = ""
    var typedCode = ""

    private var signer: DeviceSigner?

    /// Format exactly as the desktop does: one decimal place, trailing zero
    /// and point trimmed, so 211.4 reads 211.4 and 250.0 reads 250.
    static func format(_ watts: Double) -> String {
        var text = String(format: "%.1f", watts)
        if text.hasSuffix(".0") { text.removeLast(2) }
        return text
    }

    func start() async {
        guard signer == nil else { return }
        do {
            let signer = try DeviceKeyStore.loadOrCreate()
            self.signer = signer
            let key = signer.publicKeyX963
            provenance = [
                signer.isHardwareBacked ? "secure enclave" : "keychain (software key)",
                "x963 \(key.count)B prefix 0x\(String(format: "%02x", key.first ?? 0))",
                AppConfiguration.apiBaseURL.absoluteString,
            ].joined(separator: "  •  ")
        } catch {
            state = .failed(String(describing: error))
            return
        }
        // A code supplied by the launch environment is how the automated
        // end-to-end run drives this without typing on a simulator keyboard.
        // Debug only: a shipped build always asks the rider.
        #if DEBUG
        if let code = ProcessInfo.processInfo.environment["WATTRACKER_PAIRING_CODE"],
           !code.isEmpty {
            await pair(using: code)
            return
        }
        #endif
        state = .needsCode
    }

    func pairWithTypedCode() async {
        await pair(using: typedCode)
    }

    private func pair(using code: String) async {
        guard let signer else {
            state = .failed("No signing key")
            return
        }
        let client = CloudClient(baseURL: AppConfiguration.apiBaseURL, signer: signer)
        do {
            state = .working("Pairing")
            let device = try await client.pair(code: code)
            // The context handed back by pairing would already work. Refresh
            // anyway: signing a request is the integration this whole
            // skeleton exists to test, and doing it here means a broken
            // canonical request fails loudly on the first run rather than
            // five minutes later when the first context expires.
            state = .working("Signing a refresh")
            let context = try await client.refreshReaderContext(for: device)
            state = .working("Reading the profile")
            let watts = try await client.fetchFTPWatts(
                readerContext: context, device: device
            )
            state = .ftp(watts)
        } catch {
            state = .failed(String(describing: error))
        }
    }
}
