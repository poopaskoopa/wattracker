import SwiftUI
import UIKit

@main
struct WatTrackerApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
                // Dark only. The palette in Theme/Palette.swift is the desktop
                // app's dark theme and there is no light variant to fall back
                // to, so the appearance is forced rather than followed.
                .preferredColorScheme(.dark)
                .onAppear(perform: requestLandscape)
        }
    }

    /// Ask the window scene for landscape, on iPhone only.
    ///
    /// `UISupportedInterfaceOrientations` in Info.plist is the declaration
    /// that matters, and on iPhone it is enough: a portrait iPhone renders
    /// this app rotated ninety degrees, which is the restriction working.
    ///
    /// On iPadOS 26 it is not enough, and neither is this. Measured under
    /// issue #171 on the iPad Pro 11-inch simulator running iPadOS 26.4: with
    /// the orientation list set, `UIRequiresFullScreen` true, AND this request
    /// made, the app still lays out portrait on a portrait iPad. An app built
    /// against the iOS 26 SDK joins the new iPad windowing system, where a
    /// window is resizable and an orientation list no longer locks it.
    ///
    /// Issue #158 decided what to do about that: the iPad adapts. It declares
    /// all four orientations and every iPad layout must be correct in
    /// portrait. That decision is why this request is now gated to the phone.
    /// On iPad the call was already ineffective, but it is worse than
    /// ineffective now -- if it ever started working it would yank the rider's
    /// window out of a portrait layout the app fully supports. The phone keeps
    /// it: it is the API the platform documents for this, it costs nothing,
    /// and nobody should read it as a guarantee on either idiom.
    private func requestLandscape() {
        guard UIDevice.current.userInterfaceIdiom == .phone else { return }
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
