package com.wattracker.android.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

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
    background = Palette.bg,
    onBackground = Palette.text,
    surface = Palette.bg,
    onSurface = Palette.text,
    surfaceVariant = Palette.surfaceInset,
    onSurfaceVariant = Palette.muted,
    outline = Palette.surfaceBorder,
    error = Palette.alert,
    onError = Palette.textBright,
)

@Composable
fun WatTrackerTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = wtDarkColorScheme,
        content = content,
    )
}
