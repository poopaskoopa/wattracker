package com.wattracker.android.ui.theme

import androidx.compose.ui.graphics.Color

/**
 * The desktop palette, ported.
 *
 * Every value here is copied value-for-value from the `:root` block of
 * `wattracker/web/static/style.css`, which is the single source of truth for
 * what this product looks like. The names match the CSS custom properties
 * they came from, so a change on one side is greppable on the other. There is
 * deliberately no second, Android-only palette: the rider looks at the web
 * app on a laptop and this app on a phone, often in the same session, and two
 * palettes that drift apart read as two products.
 *
 * Mirrors `ios/WatTracker/WatTracker/Theme/Palette.swift` value for value.
 * This app is dark-only; there is no light variant.
 */
object Palette {
    // ---- surfaces ----
    /** The window background. Everything sits on this. */
    val bg = hex(0x0f1419)
    /** A panel: the standard raised container. */
    val panel = hex(0x1a2028)
    /** Raised one step off [panel]: table zebra, hovered rows, chips. */
    val surface2 = hex(0x212934)
    /** Recessed below [panel]: inputs, code blocks, progress tracks. */
    val surfaceInset = hex(0x10161d)
    /** The panel edge. Panels sit on a [bg] only ~7% darker than themselves, so
     * without this hairline they have no readable edge at all. */
    val surfaceBorder = hex(0x2a333d)

    // ---- ink ----
    val text = hex(0xe6e6e6)
    /** Brighter than body text, for numbers that get read at a glance from the
     * bike. Chart ticks on the web side. */
    val textBright = hex(0xf5f7fa)
    val muted = hex(0x8a94a0)

    // ---- brand / status ----
    /** UI chrome only, never a data-series colour. */
    val accent = hex(0xf2a900)
    /** Ink for anything sitting on an accent/ok/alert fill. White on this gold
     * is ~1.9:1 and unreadable; near-black is ~11:1. */
    val onAccent = hex(0x1a1a1a)
    val ok = hex(0x4caf7d)
    val alert = hex(0xe05252)
    /** Heart rate. The one series colour the chrome is allowed to borrow. */
    val hr = hex(0xd55181)

    /** 0xRRGGBB, because the CSS this came from is written that way and a
     * transcription is easier to check against the source than three
     * floating-point components would be. */
    private fun hex(value: Int): Color = Color(value.toLong() or 0xFF000000L)
}
