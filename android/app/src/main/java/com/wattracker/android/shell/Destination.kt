package com.wattracker.android.shell

import androidx.annotation.StringRes
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.ui.graphics.vector.ImageVector
import com.wattracker.android.R
import com.wattracker.android.ui.theme.wtBarChart
import com.wattracker.android.ui.theme.wtCalendarMonth
import com.wattracker.android.ui.theme.wtPedalBike
import com.wattracker.android.ui.theme.wtSpeed

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
    Dashboard(R.string.destination_dashboard, wtSpeed),
    Activities(R.string.destination_activities, wtPedalBike),
    Calendar(R.string.destination_calendar, wtCalendarMonth),
    Volume(R.string.destination_volume, wtBarChart),
    Settings(R.string.destination_settings, Icons.Filled.Settings),
    ;

    /** The nav-graph route for this destination. Computed once at class-load. */
    val route: String = name.lowercase()

    companion object {
        fun fromRoute(route: String?): Destination? =
            entries.firstOrNull { it.route == route }
    }
}
