import SwiftUI

/// The app shell: one navigation model, two presentations.
///
/// ## Why the iPhone gets a leading rail and not a bottom `TabView`
///
/// This app is landscape on iPhone, so the viewport is wide and short --
/// roughly 874x402pt on an iPhone 17 Pro before safe areas. Vertical space is
/// the scarce axis and horizontal space is the abundant one.
///
/// A bottom tab bar costs about 49pt of that 402pt height, on every screen,
/// forever: more than 12% of the scarce axis. A 64pt leading rail costs 64 of
/// 874pt, about 7% of the abundant axis. On a wide, short viewport chrome
/// belongs on the long edge. The rail also sits under the left thumb, which is
/// where the device is actually held in landscape, while a bottom bar in
/// landscape sits under neither hand.
///
/// This is a deliberate departure from the platform default, and the default
/// is the right call in portrait. It is not right here.
///
/// ## Why the iPad gets a `NavigationSplitView` that must survive portrait
///
/// iPadOS 26 does not honour a landscape-only orientation list (measured under
/// issue #171, see `docs/ios-walking-skeleton.md`). An iPad window is
/// resizable and can be portrait whatever `Info.plist` asks for, so #158
/// decided the app adapts instead of pretending: the iPad declares all four
/// orientations and every iPad layout has to be *correct* in portrait, though
/// not necessarily optimal. `NavigationSplitView` is chosen partly for that --
/// it collapses to a push navigation stack on its own when the window is
/// narrow, which is the correct portrait behaviour rather than a special case
/// this code has to write.
struct RootView: View {
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass

    /// Optional because `NavigationSplitView`'s sidebar `List` selection has to
    /// be able to go nil when it collapses; the rail path treats nil as
    /// `.dashboard`.
    @State private var selection: Destination? = .dashboard

    var body: some View {
        Group {
            if usesSplitView {
                splitView
            } else {
                railLayout
            }
        }
        .tint(Palette.accent)
    }

    /// A regular width **on an iPad**.
    ///
    /// The obvious predicate -- `horizontalSizeClass == .regular` -- is not
    /// quite right, because a Max-sized iPhone in landscape also reports a
    /// regular horizontal size class. That phone has the same ~430pt of height
    /// every other landscape iPhone has, so the reasoning above applies to it
    /// unchanged and it should get the rail, not a split view. Checking the
    /// idiom as well keeps the split view on the device the split view was
    /// designed for. A narrow iPad window (Slide Over, a small Stage Manager
    /// window) is compact and correctly falls through to the rail.
    private var usesSplitView: Bool {
        horizontalSizeClass == .regular
            && UIDevice.current.userInterfaceIdiom == .pad
    }

    private var splitView: some View {
        NavigationSplitView {
            List(Destination.allCases, selection: $selection) { destination in
                NavigationLink(value: destination) {
                    Label(destination.title, systemImage: destination.symbol)
                }
                .listRowBackground(Palette.bg)
            }
            .listStyle(.sidebar)
            .scrollContentBackground(.hidden)
            .background(Palette.bg)
            .navigationTitle("WatTracker")
        } detail: {
            NavigationStack {
                screen(for: selection ?? .dashboard)
                    .toolbarBackground(Palette.panel, for: .navigationBar)
            }
        }
    }

    private var railLayout: some View {
        // The rail is allowed to run under the sensor housing on the leading
        // edge so its surface reaches the physical edge of the display; it
        // re-applies the inset to its own buttons. Everything else keeps the
        // safe area it was given.
        GeometryReader { proxy in
            HStack(spacing: 0) {
                SideRail(
                    selection: Binding(
                        get: { selection ?? .dashboard },
                        set: { selection = $0 }
                    ),
                    leadingInset: proxy.safeAreaInsets.leading
                )
                screen(for: selection ?? .dashboard)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .ignoresSafeArea(edges: .leading)
        }
        .background(Palette.bg.ignoresSafeArea())
    }

    @ViewBuilder
    private func screen(for destination: Destination) -> some View {
        switch destination {
        case .dashboard: DashboardScreen()
        case .activities: ActivitiesScreen()
        case .calendar: CalendarScreen()
        case .volume: VolumeScreen()
        case .settings: SettingsScreen()
        }
    }
}

#Preview {
    // The gate is what `AppGate` injects in the running app; `SettingsScreen`
    // reads it from the environment, so a preview without one would trap.
    // Constructing one is free -- nothing touches the keychain until `start`.
    RootView()
        .environment(SessionGate())
        .preferredColorScheme(.dark)
}
