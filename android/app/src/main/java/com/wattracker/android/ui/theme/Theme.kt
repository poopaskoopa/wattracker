package com.wattracker.android.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * Dark-only, fixed palette.
 *
 * [Palette] is the source of truth (the web `:root` block). The scheme below
 * starts from [darkColorScheme] -- the app is dark-only and there is no light
 * variant, upstream or here -- and overrides the roles the shell actually
 * uses, so stock Material components (the navigation rail, the drawer,
 * buttons) render on the right surfaces. Screens style themselves with
 * [Palette] directly, exactly as the iOS screens do with `Palette.swift`, so
 * this mapping never has to express the design.
 *
 * No dynamic color: the product has one palette across web, iOS and Android,
 * and Material You recoloring would make the phone a second one.
 */
private val wtDarkColorScheme = darkColorScheme(
    primary = Palette.accent,
    onPrimary = Palette.onAccent,
    primaryContainer = Palette.accent.copy(alpha = 0.16f),
    onPrimaryContainer = Palette.textBright,
    secondary = Palette.ok,
    onSecondary = Palette.onAccent,
    tertiary = Palette.hr,
    onTertiary = Palette.onAccent,
    tertiaryContainer = Palette.hr.copy(alpha = 0.16f),
    onTertiaryContainer = Palette.textBright,
    background = Palette.bg,
    onBackground = Palette.text,
    surface = Palette.bg,
    onSurface = Palette.text,
    surfaceVariant = Palette.surfaceInset,
    onSurfaceVariant = Palette.muted,
    surfaceContainerLowest = Palette.bg,
    surfaceContainerLow = Palette.panel,
    surfaceContainer = Palette.panel,
    surfaceContainerHigh = Palette.surface2,
    surfaceContainerHighest = Palette.surface2,
    outline = Palette.surfaceBorder,
    outlineVariant = Palette.surfaceBorder,
    scrim = Color.Black.copy(alpha = 0.6f),
    inverseSurface = Palette.text,
    error = Palette.alert,
    onError = Palette.onAccent,
    // The roles nothing in the shell touches yet. Left at the stock
    // darkColorScheme() defaults they are tinted M3 greys, so the first Step 2
    // component to reach for one (a Snackbar, a tonal surface) would render a
    // single stray lilac panel in an otherwise fixed palette (#260). Cheaper to
    // map them now than to hunt one down later.
    surfaceBright = Palette.surface2,
    surfaceDim = Palette.bg,
    inverseOnSurface = Palette.bg,
    // The one role this palette cannot fill well: inverseSurface is light
    // (Palette.text), and there is no dark accent variant to place on it -- the
    // palette is value-for-value from the web `:root`, so inventing one is not
    // an option here. Step 2 should style Snackbar actions from [Palette]
    // directly rather than rely on this pairing.
    inversePrimary = Palette.accent,
)

@Composable
fun WatTrackerTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = wtDarkColorScheme,
        content = content,
    )
}
