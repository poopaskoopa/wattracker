import SwiftUI

/// The leading icon rail: the iPhone-landscape navigation chrome.
///
/// 64pt of the long edge, full height, five icon+label buttons. See the
/// rationale comment in `RootView` for why this exists instead of a bottom tab
/// bar.
struct SideRail: View {
    @Binding var selection: Destination

    /// How far the sensor housing intrudes on this edge. `railLayout` measures
    /// it once and hands it down, because a `GeometryReader` here would have
    /// to be inside the very frame it is trying to size.
    var leadingInset: CGFloat = 0

    /// Wide enough for a 22pt symbol plus a readable caption under it, narrow
    /// enough to stay a rail rather than a sidebar. 64/874 is ~7% of the
    /// abundant axis on an iPhone 17 Pro in landscape.
    private let width: CGFloat = 64

    var body: some View {
        // Scrolling matters here: at the largest accessibility text sizes the
        // five labels are taller than a 402pt landscape phone, and a rail that
        // clips its last item hides Settings entirely.
        ScrollView(.vertical, showsIndicators: false) {
            VStack(spacing: 4) {
                ForEach(Destination.allCases) { destination in
                    button(for: destination)
                }
            }
            .padding(.vertical, 8)
        }
        .scrollBounceBehavior(.basedOnSize)
        // The buttons stay clear of the sensor housing, but the rail's colour
        // does not: `railLayout` lets this view extend into the leading safe
        // area and this padding puts the content back. Without it the housing
        // strip renders in the window background and the rail looks like it is
        // floating a few millimetres off the edge of the screen.
        //
        // Explicit padding rather than `.safeAreaPadding(.leading)`, which
        // would be a no-op here: `railLayout` has already consumed that edge
        // with `.ignoresSafeArea`, so there is no inset left for it to read
        // and the buttons would slide back under the housing.
        .padding(.leading, leadingInset)
        .frame(width: width + leadingInset)
        .frame(maxHeight: .infinity)
        .background(Palette.panel.ignoresSafeArea())
        .overlay(alignment: .trailing) {
            Rectangle()
                .fill(Palette.surfaceBorder)
                .frame(width: 1)
                .ignoresSafeArea()
        }
    }

    private func button(for destination: Destination) -> some View {
        let isSelected = destination == selection
        return Button {
            selection = destination
        } label: {
            VStack(spacing: 3) {
                Image(systemName: destination.symbol)
                    .font(.system(size: 20, weight: .medium))
                    .frame(height: 24)
                Text(destination.title)
                    .font(.system(size: 10, weight: isSelected ? .semibold : .regular))
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 7)
            .foregroundStyle(isSelected ? Palette.accent : Palette.muted)
            .background {
                // Selection is carried by both a fill and the colour change,
                // never by colour alone.
                if isSelected {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(Palette.accent.opacity(0.16))
                }
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 5)
        .accessibilityLabel(destination.title)
        .accessibilityAddTraits(isSelected ? [.isButton, .isSelected] : .isButton)
    }
}
