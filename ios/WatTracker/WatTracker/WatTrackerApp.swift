import SwiftUI
import UIKit

@main
struct WatTrackerApp: App {
    var body: some Scene {
        WindowGroup {
            FTPScreen()
                .preferredColorScheme(.dark)
                .onAppear(perform: requestLandscape)
        }
    }

    /// Ask the window scene for landscape.
    ///
    /// `UISupportedInterfaceOrientations` in Info.plist is the declaration
    /// that matters, and on iPhone it is enough: a portrait iPhone renders
    /// this app rotated ninety degrees, which is the restriction working.
    ///
    /// On iPadOS 26 it is not enough, and neither is this. Measured on the
    /// iPad Pro 11-inch simulator running iPadOS 26.4: with the orientation
    /// list set, `UIRequiresFullScreen` true, AND this request made, the app
    /// still lays out portrait on a portrait iPad. An app built against the
    /// iOS 26 SDK joins the new iPad windowing system, where a window is
    /// resizable and an orientation list no longer locks it. The request is
    /// kept because it is the API the platform documents for this and costs
    /// nothing, but nobody should read it as a guarantee -- see ios/README.md.
    /// Every iPad layout in this app has to survive a portrait window.
    private func requestLandscape() {
        guard let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first else { return }
        scene.requestGeometryUpdate(
            .iOS(interfaceOrientations: .landscape)
        ) { error in
            // Nothing to recover: a refused request means the rider gets a
            // portrait window, which is a worse layout and not a failure.
            NSLog("WatTracker: landscape request refused: \(error)")
        }
    }
}
