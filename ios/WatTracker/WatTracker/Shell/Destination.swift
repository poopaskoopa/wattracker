import SwiftUI

/// The five top-level places in the app.
///
/// This is the whole navigation model. Both shells -- the iPad split view and
/// the iPhone rail -- render this list and nothing else, so adding a
/// destination is one case here plus one screen, and the two idioms cannot
/// fall out of sync with each other.
enum Destination: String, CaseIterable, Identifiable, Hashable {
    case dashboard
    case activities
    case calendar
    case volume
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .dashboard: "Dashboard"
        case .activities: "Activities"
        case .calendar: "Calendar"
        case .volume: "Volume"
        case .settings: "Settings"
        }
    }

    /// SF Symbols only: they ship with the system, scale with Dynamic Type and
    /// cost nothing to bundle. The rail is icon-first, so these carry more
    /// weight than they would in a plain list.
    var symbol: String {
        switch self {
        case .dashboard: "gauge.with.dots.needle.33percent"
        case .activities: "figure.outdoor.cycle"
        case .calendar: "calendar"
        case .volume: "chart.bar.fill"
        case .settings: "gearshape.fill"
        }
    }
}
