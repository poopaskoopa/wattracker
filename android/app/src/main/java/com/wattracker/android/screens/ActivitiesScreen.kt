package com.wattracker.android.screens

import android.content.res.Configuration
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.viewmodel.compose.viewModel
import com.wattracker.android.R
import com.wattracker.android.WatTrackerApp
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.ReadModel
import com.wattracker.android.screens.activities.ActivitiesUiState
import com.wattracker.android.screens.activities.ActivitiesViewModel
import com.wattracker.android.screens.activities.RideDetailPane
import com.wattracker.android.screens.activities.RideFormatting
import com.wattracker.android.screens.activities.RideSummary
import com.wattracker.android.screens.activities.updatedAt
import com.wattracker.android.shell.Panel
import com.wattracker.android.shell.ScrollableScreenScaffold
import com.wattracker.android.ui.theme.Palette
import kotlinx.coroutines.CancellationException

/**
 * Activities (#197): the ride list and the ride detail.
 *
 * Layout decision (issue #197 asks for it to be stated): on the wide, short
 * viewports -- a phone in landscape and any tablet -- the list and the
 * detail sit side by side, because width is the axis that is not scarce
 * there and push navigation would cost the rider a round trip on the
 * scarce one (back to a list they were already reading, to choose the next
 * ride). A phone in portrait has no spare width, so the detail is pushed
 * as its own screen. Tablet is the same arrangement, wider.
 */
@Composable
fun ActivitiesScreen(
    onOpenDetail: (Int) -> Unit = {},
    onNavigateToSettings: (() -> Unit)? = null,
) {
    val context = LocalContext.current
    val activeSource by WatTrackerApp.dataSourceFlow.collectAsState()

    // The read model waits on the app's blocking setup (Room, the signing key,
    // the credential vault), which runs off the main thread; it is
    // awaited here, never blocked on. A setup failure is shown, not thrown
    // into composition.
    val readModelResult by produceState<Result<ReadModel>?>(initialValue = null, activeSource) {
        value = try {
            Result.success(WatTrackerApp.readModel(context))
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    val currentReadModel = readModelResult?.getOrNull()
    if (currentReadModel == null) {
        ScrollableScreenScaffold(
            title = stringResource(R.string.destination_activities),
            subtitle = stringResource(R.string.screen_activities_subtitle),
        ) {
            if (readModelResult == null) {
                Text(
                    text = stringResource(R.string.dashboard_starting),
                    style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                )
            } else {
                Text(
                    text = stringResource(R.string.dashboard_start_failed),
                    style = TextStyle(fontSize = 14.sp, color = Palette.alert),
                )
            }
        }
        return
    }

    val factory = remember(currentReadModel, activeSource) {
        object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                return ActivitiesViewModel(currentReadModel) as T
            }
        }
    }
    val viewModel: ActivitiesViewModel = viewModel(key = activeSource.name, factory = factory)
    val uiState by viewModel.uiState.collectAsState()

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, viewModel) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                viewModel.refresh()
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
        }
    }

    val configuration = LocalConfiguration.current
    val wideShort = configuration.orientation == Configuration.ORIENTATION_LANDSCAPE ||
        configuration.smallestScreenWidthDp >= 600

    if (wideShort) {
        TwoPaneActivities(
            uiState = uiState,
            viewModel = viewModel,
            readModel = currentReadModel,
            sourceName = activeSource.name,
            // The list is wider on a tablet than on a phone in landscape.
            listWidth = if (configuration.smallestScreenWidthDp >= 600) 420.dp else 340.dp,
            onNavigateToSettings = onNavigateToSettings,
        )
    } else {
        SinglePaneActivities(
            uiState = uiState,
            viewModel = viewModel,
            onOpenDetail = onOpenDetail,
            onNavigateToSettings = onNavigateToSettings,
        )
    }
}

// MARK: - Single pane (phone portrait)

@Composable
private fun SinglePaneActivities(
    uiState: ActivitiesUiState,
    viewModel: ActivitiesViewModel,
    onOpenDetail: (Int) -> Unit,
    onNavigateToSettings: (() -> Unit)?,
) {
    PullToRefreshBox(
        isRefreshing = uiState.isRefreshing,
        onRefresh = { viewModel.pullToRefresh() },
        modifier = Modifier
            .fillMaxSize()
            .background(Palette.bg),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(16.dp),
        ) {
            ActivitiesListContent(
                uiState = uiState,
                onRideClick = { ride -> onOpenDetail(ride.activityId) },
                onNavigateToSettings = onNavigateToSettings,
                selectable = false,
            )
        }
    }
}

// MARK: - Two panes (phone landscape, tablet)

@Composable
private fun TwoPaneActivities(
    uiState: ActivitiesUiState,
    viewModel: ActivitiesViewModel,
    readModel: ReadModel,
    sourceName: String,
    listWidth: androidx.compose.ui.unit.Dp,
    onNavigateToSettings: (() -> Unit)?,
) {
    Row(modifier = Modifier.fillMaxSize()) {
        PullToRefreshBox(
            isRefreshing = uiState.isRefreshing,
            onRefresh = { viewModel.pullToRefresh() },
            modifier = Modifier
                .width(listWidth)
                .fillMaxHeight(),
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Palette.bg)
                    .padding(16.dp),
            ) {
                ActivitiesListContent(
                    uiState = uiState,
                    onRideClick = { ride -> viewModel.select(ride.id) },
                    onNavigateToSettings = onNavigateToSettings,
                    selectable = true,
                )
            }
        }

        Box(
            modifier = Modifier
                .fillMaxHeight()
                .width(1.dp)
                .background(Palette.surfaceBorder),
        )

        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(Palette.bg),
        ) {
            val ride = uiState.selectedRide
            if (ride != null) {
                RideDetailPane(
                    activityId = ride.activityId,
                    readModel = readModel,
                    sourceName = sourceName,
                    modifier = Modifier.fillMaxSize(),
                )
            } else {
                SelectOnePanel()
            }
        }
    }
}

// MARK: - The shared pieces

/**
 * The list's header: the title, the "Offline" badge when the rides came from
 * the cache, the "Newest first · updated" line, the waking label, and an
 * error line when the list is still on screen. Mirrors the iOS header.
 */
@Composable
private fun ActivitiesHeader(uiState: ActivitiesUiState) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                text = stringResource(R.string.destination_activities),
                style = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            Spacer(modifier = Modifier.weight(1f))
            if (uiState.source == CloudSnapshot.Source.cache) {
                Text(
                    text = stringResource(R.string.activities_offline_badge),
                    style = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.Medium, color = Palette.muted),
                )
            }
        }
        Text(
            text = if (uiState.asOf > 0) {
                stringResource(R.string.activities_newest_first_updated, updatedAt(uiState.asOf))
            } else {
                stringResource(R.string.activities_every_ride)
            },
            style = TextStyle(fontSize = 14.sp, color = Palette.muted),
        )
        if (uiState.isWaking) {
            Text(
                text = stringResource(R.string.dashboard_waking_title),
                style = TextStyle(fontSize = 12.sp, color = Palette.muted),
            )
        }
        if (uiState.error != null && uiState.rides.isNotEmpty()) {
            Text(
                text = uiState.error!!,
                style = TextStyle(fontSize = 12.sp, color = Palette.alert),
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun SyncingNotice() {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .background(Palette.surfaceInset)
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(
            text = stringResource(R.string.activities_syncing),
            style = TextStyle(fontSize = 12.sp, color = Palette.muted),
        )
    }
}

/**
 * The list's content: the header, the syncing banner, and the rows or the
 * states. The rides are rendered lazily (only the visible rows are
 * composed), so a long history does not hold itself in memory at the UI
 * layer; a hidden ride is absent from [uiState.rides] outright, never
 * greyed.
 *
 * The empty states sit in a scrollable box so pull to refresh still works
 * with nothing to scroll -- exactly the "cloud waking, nothing cached"
 * state the refresh exists for. With rows on screen the [LazyColumn] is
 * the scrollable the box pulls on.
 */
@Composable
private fun ActivitiesListContent(
    uiState: ActivitiesUiState,
    onRideClick: (RideSummary) -> Unit,
    onNavigateToSettings: (() -> Unit)?,
    selectable: Boolean,
) {
    Column(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        ActivitiesHeader(uiState)
        if (uiState.isLoading && uiState.rides.isNotEmpty()) {
            SyncingNotice()
        }
        if (uiState.rides.isEmpty()) {
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState()),
            ) {
                ActivitiesStates(
                    uiState = uiState,
                    onNavigateToSettings = onNavigateToSettings,
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                items(uiState.rides, key = { it.id }) { ride ->
                    ActivityRow(
                        ride = ride,
                        selected = selectable && ride.id == uiState.selectedId,
                        onClick = { onRideClick(ride) },
                    )
                }
            }
        }
    }
}

/**
 * The list's honest states, shown only when there are no rides on screen
 * (the caller guarantees it): the unpaired/removed cards, a failed load
 * with no cache, the waking notice, the first-load spinner, and the empty
 * history.
 */
@Composable
private fun ActivitiesStates(
    uiState: ActivitiesUiState,
    onNavigateToSettings: (() -> Unit)?,
) {
    when {
        uiState.deviceState == CloudSession.DeviceState.removed -> {
            StateCard(
                title = stringResource(R.string.activities_removed_title),
                note = stringResource(R.string.activities_removed_note),
                onNavigateToSettings = onNavigateToSettings,
            )
        }

        !uiState.isPaired || uiState.deviceState == CloudSession.DeviceState.unpaired -> {
            StateCard(
                title = stringResource(R.string.activities_unpaired_title),
                note = stringResource(R.string.activities_unpaired_note),
                onNavigateToSettings = onNavigateToSettings,
            )
        }

        uiState.error != null -> {
            // A failed load with no cache to serve says so; "no activities"
            // is the answer to an empty history, not to a failure.
            Panel {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(
                        text = stringResource(R.string.activities_unavailable),
                        style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                    )
                    Text(
                        text = uiState.error!!,
                        style = TextStyle(fontSize = 13.sp, color = Palette.alert),
                    )
                }
            }
        }

        uiState.isWaking -> {
            WakingCardInline()
        }

        uiState.isLoading -> {
            LoadingCard()
        }

        else -> {
            // An empty history, not a failure.
            Panel {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(
                        text = stringResource(R.string.activities_none_title),
                        style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                    )
                    Text(
                        text = stringResource(R.string.activities_none_note),
                        style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                    )
                }
            }
        }
    }
}

/**
 * One ride: the date and duration on top, the metrics in an adaptive row
 * below. Mirrors the iOS `ActivityRow`.
 */
@Composable
private fun ActivityRow(
    ride: RideSummary,
    selected: Boolean,
    onClick: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(if (selected) Palette.surface2 else Palette.bg)
            .clickable(onClick = onClick)
            .padding(vertical = 7.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Row(verticalAlignment = Alignment.Top) {
            Text(
                // Formatted once when the ride was read, not on every row
                // composition: a 300-ride list recomposes rows on scroll.
                text = ride.dateText.ifEmpty { stringResource(R.string.ride_unknown_date) },
                style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            Spacer(modifier = Modifier.weight(1f))
            Text(
                text = RideFormatting.duration(ride.summary.durationS),
                style = TextStyle(fontSize = 14.sp, color = Palette.muted),
            )
        }
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            RideMetric(label = stringResource(R.string.metric_dist), value = RideFormatting.distance(ride.summary.distanceM))
            RideMetric(label = stringResource(R.string.metric_avg), value = RideFormatting.watts(ride.summary.avgPower))
            RideMetric(label = stringResource(R.string.metric_np), value = RideFormatting.watts(ride.summary.np))
            RideMetric(label = stringResource(R.string.metric_if), value = RideFormatting.decimal(ride.summary.intensityFactor, 2))
            RideMetric(label = stringResource(R.string.metric_tss), value = RideFormatting.decimal(ride.summary.tss, 0))
            RideMetric(label = stringResource(R.string.metric_rpe), value = RideFormatting.decimal(ride.summary.rpe, 0))
        }
    }
}

@Composable
private fun RideMetric(label: String, value: String) {
    Column(
        modifier = Modifier.widthIn(min = 66.dp),
        verticalArrangement = Arrangement.spacedBy(1.dp),
    ) {
        Text(
            text = label,
            style = TextStyle(fontSize = 10.sp, fontWeight = FontWeight.SemiBold, color = Palette.muted),
        )
        Text(
            text = value,
            style = TextStyle(fontSize = 14.sp, color = Palette.text),
        )
    }
}

/** The two-pane's empty detail side. */
@Composable
private fun SelectOnePanel() {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                text = stringResource(R.string.activities_select_one_title),
                style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.muted),
            )
            Text(
                text = stringResource(R.string.activities_select_one_note),
                style = TextStyle(fontSize = 13.sp, color = Palette.muted),
            )
        }
    }
}

@Composable
private fun LoadingCard() {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(140.dp)
            .background(Palette.surfaceInset),
        contentAlignment = Alignment.Center,
    ) {
        CircularProgressIndicator(
            modifier = Modifier.size(24.dp),
            color = Palette.accent,
            strokeWidth = 2.dp,
        )
    }
}

@Composable
private fun WakingCardInline() {
    Panel {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                text = stringResource(R.string.dashboard_waking_title),
                style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            Text(
                text = stringResource(R.string.dashboard_waking_notice),
                style = TextStyle(fontSize = 13.sp, color = Palette.muted),
            )
        }
    }
}

/**
 * The unpaired/removed card: the screen's honest terminal states, with the
 * same settings call to action the dashboard uses.
 */
@Composable
private fun StateCard(
    title: String,
    note: String,
    onNavigateToSettings: (() -> Unit)?,
) {
    Panel {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                text = title,
                style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            Text(
                text = note,
                style = TextStyle(fontSize = 13.sp, color = Palette.muted),
            )
            if (onNavigateToSettings != null) {
                OutlinedButton(onClick = onNavigateToSettings) {
                    Text(stringResource(R.string.dashboard_pair_cta))
                }
            }
        }
    }
}
