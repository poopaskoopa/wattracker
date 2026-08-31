import SwiftUI

/// The desktop palette, ported.
///
/// Every value here is copied from the `:root` block of
/// `wattracker/web/static/style.css`, which is the single source of truth for
/// what this product looks like. The names match the CSS custom properties
/// they came from, so a change on one side is greppable on the other. There is
/// deliberately no second, iOS-only palette: the rider looks at the web app on
/// a laptop and this app on a phone, often in the same session, and two
/// palettes that drift apart read as two products.
///
/// This app is dark-only. `WatTrackerApp` forces `.preferredColorScheme(.dark)`
/// rather than defining a light variant, because the screens are read next to a
/// trainer in a room that is usually dim, and a half-finished light theme is
/// worse than none.
enum Palette {
    // ---- surfaces ----
    /// The window background. Everything sits on this.
    static let bg = hex(0x0f1419)
    /// A panel: the standard raised container.
    static let panel = hex(0x1a2028)
    /// Raised one step off `panel`: table zebra, hovered rows, chips.
    static let surface2 = hex(0x212934)
    /// Recessed below `panel`: inputs, code blocks, progress tracks.
    static let surfaceInset = hex(0x10161d)
    /// The panel edge. Panels sit on a `bg` only ~7% darker than themselves, so
    /// without this hairline they have no readable edge at all.
    static let surfaceBorder = hex(0x2a333d)

    // ---- ink ----
    static let text = hex(0xe6e6e6)
    /// Brighter than body text, for numbers that get read at a glance from the
    /// bike. Chart ticks on the web side.
    static let textBright = hex(0xf5f7fa)
    static let muted = hex(0x8a94a0)

    // ---- brand / status ----
    /// UI chrome only, never a data-series colour.
    static let accent = hex(0xf2a900)
    /// Ink for anything sitting on an accent/ok/alert fill. White on this gold
    /// is ~1.9:1 and unreadable; near-black is ~11:1.
    static let onAccent = hex(0x1a1a1a)
    static let ok = hex(0x4caf7d)
    static let alert = hex(0xe05252)
    /// Heart rate. The one series colour the chrome is allowed to borrow.
    static let hr = hex(0xd55181)

    /// 0xRRGGBB, because the CSS this came from is written that way and a
    /// transcription is easier to check against the source than three
    /// floating-point components would be.
    static func hex(_ value: UInt32) -> Color {
        Color(
            red: Double((value >> 16) & 0xff) / 255,
            green: Double((value >> 8) & 0xff) / 255,
            blue: Double(value & 0xff) / 255
        )
    }
}
