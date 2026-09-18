package com.wattracker.android.shell

import android.content.res.Configuration
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.selection.selectable
import androidx.compose.ui.semantics.Role
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
 * The highlight shared by every destination item in every shell -- the rail,
 * the bottom bar and the drawer rows. Selection is amber [Palette.accent], the
 * rest [Palette.muted], and the selected indicator is a 16% accent wash.
 *
 * This is written explicitly rather than left to the Material defaults, and it
 * is the *same* Palette the tablet drawer already paints, so the phone and the
 * tablet read as one product: the highlight is identical on both idioms. It is
 * deliberately not the M3 primary/primaryContainer scheme, whose white selected
 * icon and tinted pill would not match the drawer's amber.
 */
private val navSelectedColor = Palette.accent
private val navUnselectedColor = Palette.muted
private val navIndicatorColor = Palette.accent.copy(alpha = 0.16f)

/**
 * The app shell: adapts navigation presentation based on device screen size and orientation.
 *
 * - Phone Landscape: leading `NavigationRail` to maximize vertical screen space.
 * - Phone Portrait: bottom `NavigationBar` for conventional single-hand thumb navigation.
 * - Large Screen (sw >= 600dp): permanent navigation drawer sheet.
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
 * Returns true if the window's `smallestScreenWidthDp >= 600`, selecting the
 * large-screen drawer layout.
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
                            contentDescription = null,
                        )
                    },
                    label = { Text(stringResource(destination.titleRes)) },
                    colors = railItemColors(),
                )
            }
        }
        AppNavHost(
            navController,
            modifier = Modifier
                .fillMaxSize()
                .consumeWindowInsets(WindowInsets.safeDrawing.only(WindowInsetsSides.Start))
                .windowInsetsPadding(WindowInsets.safeDrawing),
        )
    }
}

/**
 * Composable because [NavigationRailItemDefaults.colors] reads the theme for its
 * remaining defaults, so it cannot be hoisted to a top-level val.
 */
@Composable
private fun railItemColors() = NavigationRailItemDefaults.colors(
    selectedIconColor = navSelectedColor,
    unselectedIconColor = navUnselectedColor,
    selectedTextColor = navSelectedColor,
    unselectedTextColor = navUnselectedColor,
    indicatorColor = navIndicatorColor,
)

@Composable
private fun PhonePortraitShell(navController: NavHostController, selected: Destination) {
    // Portrait on a phone is the conventional phone layout: chrome on the bottom
    // edge, full width, under the thumb. The rail's case (a wide, short
    // landscape viewport) does not apply, so we do not reuse it here.
    Column(modifier = Modifier.fillMaxSize()) {
        AppNavHost(
            navController,
            modifier = Modifier
                .weight(1f)
                .consumeWindowInsets(WindowInsets.safeDrawing.only(WindowInsetsSides.Bottom))
                .windowInsetsPadding(WindowInsets.safeDrawing),
        )
        NavigationBar(containerColor = Palette.panel) {
            Destination.entries.forEach { destination ->
                NavigationBarItem(
                    selected = destination == selected,
                    onClick = { navigateTo(navController, destination) },
                    icon = {
                        Icon(
                            imageVector = destination.icon,
                            contentDescription = null,
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

/** Same colours as [railItemColors] for the portrait bottom bar; a separate
 * function only because the M3 types differ between the rail and the bar. */
@Composable
private fun bottomBarItemColors() = NavigationBarItemDefaults.colors(
    selectedIconColor = navSelectedColor,
    unselectedIconColor = navUnselectedColor,
    selectedTextColor = navSelectedColor,
    unselectedTextColor = navUnselectedColor,
    indicatorColor = navIndicatorColor,
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
        content = {
            AppNavHost(
                navController,
                modifier = Modifier
                    .fillMaxSize()
                    .consumeWindowInsets(WindowInsets.safeDrawing.only(WindowInsetsSides.Start))
                    .windowInsetsPadding(WindowInsets.safeDrawing),
            )
        },
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
            .background(if (selected) navIndicatorColor else Color.Transparent)
            .selectable(selected = selected, role = Role.Tab, onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            imageVector = destination.icon,
            contentDescription = null,
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
 * destinations pops to the start route (non-inclusive) so the back button
 * returns to Dashboard rather than walking a stack of sibling screens.
 * This is the conventional Android bottom-nav / rail pattern.
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
