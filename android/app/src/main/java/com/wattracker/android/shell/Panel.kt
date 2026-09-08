package com.wattracker.android.shell

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.wattracker.android.ui.theme.Palette

/** The panel corner radius; the only place it (and the hairline) are specified. */
private val PanelShape = RoundedCornerShape(12.dp)

/**
 * The panel: a bordered container on the app background.
 *
 * This exists so the five screen stubs cannot drift into five slightly
 * different ideas of what a container looks like before the real screens
 * (#196 onward) are written. It is the Compose equivalent of the web app's
 * `.panel` rule, and it is the only place the corner radius and hairline are
 * specified. Mirrors `ios/WatTracker/WatTracker/Theme/Panel.swift`.
 */
@Composable
fun Panel(
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(Palette.panel, PanelShape)
            .border(1.dp, Palette.surfaceBorder, PanelShape)
            .padding(16.dp),
    ) {
        content()
    }
}

/**
 * The common frame every screen sits in: a title, then content, on
 * [Palette.bg].
 *
 * The title is rendered here rather than by any system bar: on the phone rail
 * path there is no bar at all -- deliberately, since a bar would cost another
 * ~48dp of the scarce vertical axis for a string the rail already shows as the
 * selected item. Rendering it in the content keeps both idioms showing the same
 * thing without a bar. Mirrors iOS `ScreenScaffold`.
 */
@Composable
fun ScreenScaffold(
    title: String,
    subtitle: String,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Palette.bg)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        horizontalAlignment = Alignment.Start,
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.SemiBold,
                color = Palette.textBright,
            )
            Text(
                text = subtitle,
                style = MaterialTheme.typography.bodyMedium,
                color = Palette.muted,
            )
        }
        content()
    }
}

/**
 * A short line of placeholder text inside a [Panel].
 *
 * Every screen in this shell is a stub. They say what they will hold and which
 * issue fills them in, and they show no fake numbers and no fake charts: a
 * placeholder that looks like data is a screenshot waiting to be mistaken for a
 * working feature. Mirrors iOS `StubPanel`.
 */
@Composable
fun StubPanel(note: String, issue: String) {
    Panel {
        Text(
            text = note,
            style = MaterialTheme.typography.bodyMedium,
            color = Palette.text,
        )
        Text(
            text = issue,
            style = MaterialTheme.typography.labelSmall,
            fontFamily = FontFamily.Monospace,
            color = Palette.muted,
        )
    }
}
