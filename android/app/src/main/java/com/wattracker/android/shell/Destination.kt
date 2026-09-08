package com.wattracker.android.shell

import androidx.annotation.StringRes
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.BarChart
import androidx.compose.material.icons.filled.CalendarMonth
import androidx.compose.material.icons.filled.PedalBike
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Speed
import androidx.compose.ui.graphics.vector.ImageVector
import com.wattracker.android.R

/**
 * The five top-level places in the app.
 *
 * This is the whole navigation model. Both shells -- the phone rail and the
 * tablet drawer -- render this list and nothing else, so adding a destination
 * is one case here plus one screen, and the two idioms cannot fall out of
 * sync with each other. Mirrors the iOS `Destination` enum; the icons follow
 * the SF Symbols it uses (gauge, bike, calendar, bar chart, gear).
 */
enum class Destination(
    @StringRes val titleRes: Int,
    val icon: ImageVector,
) {
    Dashboard(R.string.destination_dashboard, Icons.Filled.Speed),
    Activities(R.string.destination_activities, Icons.Filled.PedalBike),
    Calendar(R.string.destination_calendar, Icons.Filled.CalendarMonth),
    Volume(R.string.destination_volume, Icons.Filled.BarChart),
    Settings(R.string.destination_settings, Icons.Filled.Settings),
    ;

    /** The nav-graph route for this destination. */
    val route: String
        get() = name.lowercase()

    companion object {
        fun fromRoute(route: String?): Destination? =
            entries.firstOrNull { it.route == route }
    }
}
