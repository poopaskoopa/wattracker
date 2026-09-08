package com.wattracker.android.shell

import android.content.res.Configuration
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.NavigationRailItemDefaults
import androidx.compose.material3.PermanentDrawerSheet
import androidx.compose.material3.PermanentNavigationDrawer
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.wattracker.android.R
import com.wattracker.android.screens.ActivitiesScreen
import com.wattracker.android.screens.CalendarScreen
import com.wattracker.android.screens.DashboardScreen
import com.wattracker.android.screens.SettingsScreen
import com.wattracker.android.screens.VolumeScreen
import com.wattracker.android.ui.theme.Palette

/**
 * The app shell: one navigation model, two presentations.
 *
 * ## Why the phone gets a rail in landscape and a bottom bar in portrait
 *
 * (The arithmetic from issue #193; the iOS twin is the comment on `RootView`
 * in `ios/WatTracker/WatTracker/Shell/`.) The iOS app is landscape-only on the
 * phone; Android is not, so a phone in portrait is a real, reachable state and
 * gets the conventional phone layout -- chrome on the bottom edge, full width,
 * under the thumb. The rail is the *landscape* call: there the viewport is
 * wide and short -- roughly 900x400dp on a Pixel 9 before insets -- Vertical space is the scarce axis and horizontal space the abundant
 * one.
 *
 * A bottom bar costs ~80dp of that ~400dp height, on every screen, forever: a
 * fifth of the scarce axis. An 80dp leading rail costs 80 of ~900dp, under a
 * tenth of the abundant axis. On a wide, short viewport chrome belongs on the
 * long edge. The rail also sits under the left thumb, which is where the device
 * is actually held in landscape, while a bottom bar in landscape sits under
 * neither hand.
 *
 * This is a deliberate departure from the platform default, and the default is
 * the right call in portrait. It is not right here.
 *
 * ## Why the tablet gets a permanent drawer and must survive portrait
 *
 * targetSdk 36 means the app is built against the Android 16 large-screen
 * rules, where orientation restrictions are ignored on large screens: a
 * tablet window can be portrait whatever the manifest asks for, so the drawer
 * layout has to be *correct* in portrait, though not necessarily optimal.
 * [PermanentNavigationDrawer] is the pick because it is a list-detail
 * scaffold (the `NavigationSplitView` analogue): at the tablet's portrait
 * width the list stays visible and nothing has to be written to collapse it,
 * and the window never gets narrow enough for that to be the wrong call.
 */
@Composable
fun RootScreen() {
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val selected = Destination.fromRoute(backStackEntry?.destination?.route)
        ?: Destination.Dashboard

    if (usesLargeScreenShell()) {
        TabletShell(navController, selected)
    } else {
        PhoneShell(navController, selected)
    }
}

/**
 * Whether the current window gets the large-screen shell (drawer) or the phone
 * shell (rail).
 *
 * The iOS twin of this predicate (`RootView.usesSplitView`) had to check both
 * the width size class and the idiom, because a Max-sized iPhone in landscape
 * reports a regular width class. On Android the two halves collapse into one
 * honest check: [android.content.Configuration.smallestScreenWidthDp] is the
 * smallest the *device's* short edge ever gets, so it is simultaneously the
 * idiom test (a phone, even a large one in landscape, cannot reach 600dp on
 * its short edge -- the iOS trap cannot occur here) and the size-class test
 * (the platform's tablet threshold). Note the window-size-class composables
 * were removed from Compose Foundation 1.10; `smallestScreenWidthDp` is the
 * stable configuration value they used to approximate, so this also survives
 * a narrow freeform window on a tablet -- the drawer stays, which is the
 * correct list-detail behaviour at any width the drawer was designed for.
 */
@Composable
private fun usesLargeScreenShell(): Boolean {
    return LocalConfiguration.current.smallestScreenWidthDp >= 600
}

@Composable
private fun PhoneShell(navController: NavHostController, selected: Destination) {
    if (LocalConfiguration.current.orientation == Configuration.ORIENTATION_PORTRAIT) {
        PhonePortraitShell(navController, selected)
    } else {
        PhoneLandscapeShell(navController, selected)
    }
}

@Composable
private fun PhoneLandscapeShell(navController: NavHostController, selected: Destination) {
    Row(modifier = Modifier.fillMaxSize()) {
        NavigationRail(
            modifier = Modifier.fillMaxHeight(),
            containerColor = Palette.panel,
        ) {
            Destination.entries.forEach { destination ->
                NavigationRailItem(
                    selected = destination == selected,
                    onClick = { navigateTo(navController, destination) },
                    icon = {
                        Icon(
                            imageVector = destination.icon,
                            contentDescription = stringResource(destination.titleRes),
                        )
                    },
                    label = { Text(stringResource(destination.titleRes)) },
                    colors = railItemColors(),
                )
            }
        }
        AppNavHost(navController, modifier = Modifier.fillMaxSize())
    }
}

/**
 * The highlight shared by every destination item in every shell -- the rail,
 * the bottom bar and the drawer rows. Selection is amber [Palette.accent], the
 * rest [Palette.muted], and the selected indicator is a 16% accent wash.
 *
 * This is written explicitly rather than left to the Material defaults, and it
 * is the *same* Palette the tablet drawer already paints, so the phone and the
 * tablet read as one product: the highlight is identical on both idioms. It is
 * deliberately not the M3 primary/primaryContainer scheme, whose white selected
 * icon and tinted pill would not match the drawer's amber.
 *
 * Composable because [NavigationRailItemDefaults.colors] reads the theme for its
 * remaining defaults, so it cannot be hoisted to a top-level val.
 */
@Composable
private fun railItemColors() = NavigationRailItemDefaults.colors(
    selectedIconColor = Palette.accent,
    unselectedIconColor = Palette.muted,
    selectedTextColor = Palette.accent,
    unselectedTextColor = Palette.muted,
    indicatorColor = Palette.accent.copy(alpha = 0.16f),
)

@Composable
private fun PhonePortraitShell(navController: NavHostController, selected: Destination) {
    // Portrait on a phone is the conventional phone layout: chrome on the bottom
    // edge, full width, under the thumb. The rail's case (a wide, short
    // landscape viewport) does not apply, so we do not reuse it here.
    Column(modifier = Modifier.fillMaxSize()) {
        AppNavHost(navController, modifier = Modifier.weight(1f))
        NavigationBar(containerColor = Palette.panel) {
            Destination.entries.forEach { destination ->
                NavigationBarItem(
                    selected = destination == selected,
                    onClick = { navigateTo(navController, destination) },
                    icon = {
                        Icon(
                            imageVector = destination.icon,
                            contentDescription = stringResource(destination.titleRes),
                        )
                    },
                    label = { Text(stringResource(destination.titleRes)) },
                    alwaysShowLabel = true,
                    colors = bottomBarItemColors(),
                )
            }
        }
    }
}

/** Same [Palette] as [railItemColors] for the portrait bottom bar; a separate
 * function only because the M3 types differ between the rail and the bar. The
 * values must stay in sync with [railItemColors]. */
@Composable
private fun bottomBarItemColors() = NavigationBarItemDefaults.colors(
    selectedIconColor = Palette.accent,
    unselectedIconColor = Palette.muted,
    selectedTextColor = Palette.accent,
    unselectedTextColor = Palette.muted,
    indicatorColor = Palette.accent.copy(alpha = 0.16f),
)

@Composable
private fun TabletShell(navController: NavHostController, selected: Destination) {
    PermanentNavigationDrawer(
        drawerContent = {
            PermanentDrawerSheet(
                modifier = Modifier.fillMaxHeight(),
            ) {
                // The sheet's own container colour is a Material role; the
                // drawer surface is [Palette.bg] by design (the iOS sidebar
                // sits on the app background, and its detail chrome on
                // panel), so paint it explicitly.
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(Palette.bg)
                        .padding(horizontal = 12.dp, vertical = 16.dp),
                ) {
                    Text(
                        text = stringResource(R.string.app_shell_title),
                        style = MaterialTheme.typography.titleMedium,
                        color = Palette.textBright,
                        modifier = Modifier.padding(start = 12.dp, bottom = 12.dp),
                    )
                    Destination.entries.forEach { destination ->
                        DestinationRow(
                            destination = destination,
                            selected = destination == selected,
                            onClick = { navigateTo(navController, destination) },
                        )
                    }
                }
            }
        },
        content = { AppNavHost(navController, modifier = Modifier.fillMaxSize()) },
    )
}

/**
 * A drawer row: icon left of label. Selection is carried by both a fill and
 * the colour change, never by colour alone (same rule as the iOS rail button).
 */
@Composable
private fun DestinationRow(
    destination: Destination,
    selected: Boolean,
    onClick: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(if (selected) Palette.accent.copy(alpha = 0.16f) else Color.Transparent)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            imageVector = destination.icon,
            contentDescription = stringResource(destination.titleRes),
            modifier = Modifier.size(24.dp),
            tint = if (selected) Palette.accent else Palette.muted,
        )
        Text(
            text = stringResource(destination.titleRes),
            style = MaterialTheme.typography.labelLarge,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
            color = if (selected) Palette.accent else Palette.muted,
            modifier = Modifier.padding(start = 16.dp),
        )
    }
}

/**
 * Flat navigation: one destination per route, no stack. Switching
 * destinations pops back to the start route so the back button always exits
 * the app rather than walking a stack of screens that are siblings, not
 * levels.
 */
private fun navigateTo(navController: NavHostController, destination: Destination) {
    navController.navigate(destination.route) {
        popUpTo(navController.graph.findStartDestination().id) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}

@Composable
private fun AppNavHost(
    navController: NavHostController,
    modifier: Modifier = Modifier,
) {
    NavHost(
        navController = navController,
        startDestination = Destination.Dashboard.route,
        modifier = modifier,
    ) {
        composable(Destination.Dashboard.route) { DashboardScreen() }
        composable(Destination.Activities.route) { ActivitiesScreen() }
        composable(Destination.Calendar.route) { CalendarScreen() }
        composable(Destination.Volume.route) { VolumeScreen() }
        composable(Destination.Settings.route) { SettingsScreen() }
    }
}
