import AVFoundation
import SwiftUI
import UIKit

/// Camera permission, in the four states the pairing screen has to draw.
///
/// `unavailable` is separated from `denied` because they need different words:
/// the simulator and an iPad with no usable camera are not the rider refusing
/// anything, and offering "Open Settings" there sends them somewhere with no
/// switch to flip. Typing the code works in every one of these states, which is
/// what keeps a refused camera an inconvenience rather than a dead end.
enum CameraAccess: Equatable {
    case unavailable
    case undetermined
    case denied
    case allowed

    static var current: CameraAccess {
        guard AVCaptureDevice.default(for: .video) != nil else { return .unavailable }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: return .allowed
        case .notDetermined: return .undetermined
        default: return .denied
        }
    }

    /// Ask, once. iOS shows the prompt only the first time; afterwards this
    /// returns the standing answer without showing anything, which is why the
    /// screen has to be able to explain Settings rather than just ask again.
    static func request() async -> CameraAccess {
        guard current != .unavailable else { return .unavailable }
        _ = await AVCaptureDevice.requestAccess(for: .video)
        return current
    }
}

/// A live camera that reports QR payloads.
///
/// Deliberately thin: it starts a capture session, draws the preview, and hands
/// every distinct QR string it sees to `onCode`. It knows nothing about pairing
/// codes -- deciding whether a scanned string is one belongs with
/// `PairingCode`, and deciding what to do about it belongs with the screen.
struct QRCodeScannerView: UIViewControllerRepresentable {
    let onCode: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onCode: onCode) }

    func makeUIViewController(context: Context) -> ScannerViewController {
        let controller = ScannerViewController()
        controller.metadataDelegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: ScannerViewController, context: Context) {
        context.coordinator.onCode = onCode
    }

    final class Coordinator: NSObject, AVCaptureMetadataOutputObjectsDelegate {
        var onCode: (String) -> Void
        /// The camera reports the same symbol many times a second while it is
        /// in frame. Only a change is an event.
        private var lastReported: String?

        init(onCode: @escaping (String) -> Void) {
            self.onCode = onCode
        }

        func metadataOutput(
            _ output: AVCaptureMetadataOutput,
            didOutput metadataObjects: [AVMetadataObject],
            from connection: AVCaptureConnection
        ) {
            for object in metadataObjects {
                guard let code = object as? AVMetadataMachineReadableCodeObject,
                      code.type == .qr, let value = code.stringValue else { continue }
                guard value != lastReported else { return }
                lastReported = value
                onCode(value)
                return
            }
        }
    }
}

/// The capture session and its preview layer.
///
/// Configuration happens off the main thread because
/// `AVCaptureSession.startRunning` blocks until the camera is up -- long enough
/// to be a visible hitch on the screen that is doing it -- and the delegate
/// callbacks are asked for on `.main` because their only consumer is SwiftUI
/// state.
final class ScannerViewController: UIViewController {
    weak var metadataDelegate: AVCaptureMetadataOutputObjectsDelegate?

    private let session = AVCaptureSession()
    private let sessionQueue = DispatchQueue(label: "com.wattracker.ios.qr-session")
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private var configured = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        view.layer.addSublayer(preview)
        previewLayer = preview
        sessionQueue.async { [weak self] in self?.configure() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        // No implicit animation: the preview layer is not a view, so a bounds
        // change during a rotation would otherwise animate on its own timing
        // and lag the rest of the layout by a frame or two.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        previewLayer?.frame = view.bounds
        CATransaction.commit()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        sessionQueue.async { [weak self] in
            guard let self, self.configured, !self.session.isRunning else { return }
            self.session.startRunning()
        }
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        // Stopping matters: a running capture session keeps the camera
        // indicator lit and the radio warm for a screen nobody is looking at.
        sessionQueue.async { [weak self] in
            guard let self, self.session.isRunning else { return }
            self.session.stopRunning()
        }
    }

    private func configure() {
        guard !configured else { return }
        var usable = false
        session.beginConfiguration()
        if let camera = AVCaptureDevice.default(for: .video),
           let input = try? AVCaptureDeviceInput(device: camera),
           session.canAddInput(input) {
            session.addInput(input)
            let output = AVCaptureMetadataOutput()
            if session.canAddOutput(output) {
                session.addOutput(output)
                // `availableMetadataObjectTypes` is empty until the output has
                // a session, so this ordering is required rather than
                // stylistic: asking before `addOutput` reports nothing at all.
                if output.availableMetadataObjectTypes.contains(.qr) {
                    output.setMetadataObjectsDelegate(metadataDelegate, queue: .main)
                    output.metadataObjectTypes = [.qr]
                    usable = true
                }
            }
        }
        // Committed before starting, never inside the configuration block: a
        // session started mid-configuration is undefined behaviour, and a
        // `defer` that commits after `startRunning` reads as though it were
        // not.
        session.commitConfiguration()
        guard usable else { return }
        configured = true
        session.startRunning()
    }
}
