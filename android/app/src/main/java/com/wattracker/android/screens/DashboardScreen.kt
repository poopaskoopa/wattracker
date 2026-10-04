package com.wattracker.android.screens

import android.content.res.Configuration
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
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
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.ReadModel
import com.wattracker.android.screens.dashboard.CurveChart
import com.wattracker.android.screens.dashboard.DashboardData
import com.wattracker.android.screens.dashboard.DashboardViewModel
import com.wattracker.android.screens.dashboard.LoadChart
import com.wattracker.android.shell.Panel
import com.wattracker.android.shell.ScrollableScreenScaffold
import com.wattracker.android.ui.theme.Palette
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.CancellationException

/**
 * Dashboard (#196): training status, load chart, power curve, and recent rides.
 */
@Composable
fun DashboardScreen(
    onNavigateToSettings: (() -> Unit)? = null,
) {
    val context = LocalContext.current
    val activeSource by WatTrackerApp.dataSourceFlow.collectAsState()

    // The read model waits on the app's blocking setup (Room, Keystore,
    // EncryptedSharedPreferences), which runs off the main thread; it is
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
            title = stringResource(R.string.destination_dashboard),
            subtitle = stringResource(R.string.screen_dashboard_subtitle),
        ) {
            if (readModelResult == null) {
                LoadingDashboardCard(message = stringResource(R.string.dashboard_starting))
            } else {
                ErrorBanner(message = stringResource(R.string.dashboard_start_failed))
            }
        }
        return
    }

    val factory = remember(currentReadModel, activeSource) {
        object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                return DashboardViewModel(currentReadModel) as T
            }
        }
    }
    val viewModel: DashboardViewModel = viewModel(key = activeSource.name, factory = factory)
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

    ScrollableScreenScaffold(
        title = stringResource(R.string.destination_dashboard),
        subtitle = stringResource(R.string.screen_dashboard_subtitle),
    ) {
        val isWide = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE ||
            LocalConfiguration.current.smallestScreenWidthDp >= 600

        when {
            uiState.isStarting -> {
                LoadingDashboardCard(message = stringResource(R.string.dashboard_starting))
            }

            uiState.deviceState == CloudSession.DeviceState.removed -> {
                UnpairedStateCard(
                    title = stringResource(R.string.dashboard_removed_title),
                    note = stringResource(R.string.dashboard_removed_note),
                    onNavigateToSettings = onNavigateToSettings,
                )
            }

            !uiState.isPaired || uiState.deviceState == CloudSession.DeviceState.unpaired -> {
                UnpairedStateCard(
                    title = stringResource(R.string.dashboard_unpaired_title),
                    note = stringResource(R.string.dashboard_unpaired_note),
                    onNavigateToSettings = onNavigateToSettings,
                )
            }

            else -> {
                val data = uiState.dashboardData

                if (uiState.isLoading && data == null) {
                    LoadingDashboardCard(message = stringResource(R.string.dashboard_syncing))
                } else if (data != null && data.hasAnyData) {
                    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        if (uiState.isLoading) {
                            SyncingBanner(message = stringResource(R.string.dashboard_syncing))
                        } else if (data.source == CloudSnapshot.Source.cache) {
                            CacheBanner(asOf = data.asOf)
                        }

                        if (uiState.throttledRetrySeconds != null) {
                            ErrorBanner(message = stringResource(R.string.dashboard_throttled_notice, uiState.throttledRetrySeconds!!))
                        } else if (uiState.error != null) {
                            ErrorBanner(message = uiState.error!!)
                        }

                        // Metric Tiles Grid
                        MetricTilesGrid(data = data, isWide = isWide)

                        // Charts Section
                        if (isWide) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(12.dp),
                            ) {
                                LoadChart(
                                    points = data.points(uiState.selectedWindow),
                                    selectedWindow = uiState.selectedWindow,
                                    onWindowSelected = { window -> viewModel.setWindow(window) },
                                    modifier = Modifier.weight(1f),
                                )
                                CurveChart(
                                    curve = data.curve,
                                    ftp = data.currentFTP,
                                    modifier = Modifier.weight(1f),
                                )
                            }
                        } else {
                            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                                LoadChart(
                                    points = data.points(uiState.selectedWindow),
                                    selectedWindow = uiState.selectedWindow,
                                    onWindowSelected = { window -> viewModel.setWindow(window) },
                                    modifier = Modifier.fillMaxWidth(),
                                )
                                CurveChart(
                                    curve = data.curve,
                                    ftp = data.currentFTP,
                                    modifier = Modifier.fillMaxWidth(),
                                )
                            }
                        }

                        // Recent Rides Strip
                        if (data.activities.isNotEmpty()) {
                            RecentRidesSection(activities = data.activities.take(5))
                        }
                    }
                } else {
                    // Nothing on screen: no cache to serve. Either the load
                    // failed -- say so, "no history" is not the answer to a
                    // failure -- or there genuinely is no history yet.
                    Panel {
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            when {
                                uiState.isWaking -> {
                                    Row(
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                                    ) {
                                        CircularProgressIndicator(
                                            modifier = Modifier.size(20.dp),
                                            strokeWidth = 2.5.dp,
                                            color = Palette.accent,
                                        )
                                        Text(
                                            text = stringResource(R.string.dashboard_waking_title),
                                            style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                                        )
                                    }
                                    Text(
                                        text = uiState.error ?: stringResource(R.string.dashboard_waking_notice),
                                        style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                                    )
                                }

                                uiState.throttledRetrySeconds != null -> {
                                    Text(
                                        text = stringResource(R.string.dashboard_sync_failed),
                                        style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                                    )
                                    Text(
                                        text = stringResource(R.string.dashboard_throttled_notice, uiState.throttledRetrySeconds!!),
                                        style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                                    )
                                }

                                uiState.error != null -> {
                                    Text(
                                        text = stringResource(R.string.dashboard_sync_failed),
                                        style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                                    )
                                    Text(
                                        text = uiState.error!!,
                                        style = TextStyle(fontSize = 13.sp, color = Palette.alert),
                                    )
                                }

                                else -> {
                                    Text(
                                        text = stringResource(R.string.dashboard_no_load_history),
                                        style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun MetricTilesGrid(data: DashboardData, isWide: Boolean) {
    val items = listOf(
        stringResource(R.string.metric_ftp) to DashboardData.formatNumber(data.currentFTP, " W"),
        stringResource(R.string.metric_cp) to DashboardData.formatNumber(data.currentCP, " W"),
        stringResource(R.string.metric_wprime) to DashboardData.formatNumber(data.currentWPrime, " J"),
        stringResource(R.string.metric_weight) to DashboardData.formatNumber(data.weightKg, " kg"),
        stringResource(R.string.metric_ctl) to DashboardData.formatNumber(data.currentCTL),
        stringResource(R.string.metric_atl) to DashboardData.formatNumber(data.currentATL),
        stringResource(R.string.metric_tsb) to DashboardData.formatNumber(data.currentTSB),
    )

    val chunkSize = if (isWide) 4 else 2
    val rows = items.chunked(chunkSize)

    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        rows.forEach { rowItems ->
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                rowItems.forEach { (title, value) ->
                    MetricTile(
                        title = title,
                        value = value,
                        modifier = Modifier.weight(1f),
                    )
                }
                // Fill empty slots in last row if needed
                repeat(chunkSize - rowItems.size) {
                    Spacer(modifier = Modifier.weight(1f))
                }
            }
        }
    }
}

@Composable
private fun MetricTile(
    title: String,
    value: String,
    modifier: Modifier = Modifier,
) {
    Panel(modifier = modifier) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                text = title,
                style = TextStyle(fontSize = 11.sp, color = Palette.muted),
            )
            Text(
                text = value,
                style = TextStyle(
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold,
                    color = Palette.textBright,
                ),
            )
        }
    }
}

@Composable
private fun CacheBanner(asOf: Long) {
    val dateStr = if (asOf > 0) {
        val sdf = SimpleDateFormat("MMM d, HH:mm", Locale.getDefault())
        sdf.format(Date(asOf))
    } else {
        "—"
    }

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .background(Palette.surfaceInset, RoundedCornerShape(6.dp))
            .border(1.dp, Palette.accent.copy(alpha = 0.3f), RoundedCornerShape(6.dp))
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(
            text = stringResource(R.string.dashboard_cached_data_banner, dateStr),
            style = TextStyle(fontSize = 12.sp, color = Palette.accent),
        )
    }
}

@Composable
private fun SyncingBanner(message: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Palette.surfaceInset, RoundedCornerShape(6.dp))
            .border(1.dp, Palette.surfaceBorder, RoundedCornerShape(6.dp))
            .padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        CircularProgressIndicator(
            modifier = Modifier.size(14.dp),
            strokeWidth = 2.dp,
            color = Palette.accent,
        )
        Text(
            text = message,
            style = TextStyle(fontSize = 12.sp, color = Palette.muted),
        )
    }
}

@Composable
private fun ErrorBanner(message: String) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .background(Palette.surfaceInset, RoundedCornerShape(6.dp))
            .border(1.dp, Palette.alert.copy(alpha = 0.4f), RoundedCornerShape(6.dp))
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(
            text = message,
            style = TextStyle(fontSize = 12.sp, color = Palette.alert),
        )
    }
}

@Composable
private fun LoadingDashboardCard(message: String) {
    Panel {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            CircularProgressIndicator(
                modifier = Modifier.size(20.dp),
                strokeWidth = 2.5.dp,
                color = Palette.accent,
            )
            Text(
                text = message,
                style = TextStyle(fontSize = 14.sp, color = Palette.muted),
            )
        }
    }
}

@Composable
private fun UnpairedStateCard(
    title: String,
    note: String,
    onNavigateToSettings: (() -> Unit)?,
) {
    Panel {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(
                text = title,
                style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.Bold, color = Palette.textBright),
            )
            Text(
                text = note,
                style = TextStyle(fontSize = 13.sp, color = Palette.muted),
            )
            if (onNavigateToSettings != null) {
                OutlinedButton(
                    onClick = onNavigateToSettings,
                    modifier = Modifier.padding(top = 4.dp),
                ) {
                    Text(text = stringResource(R.string.dashboard_pair_cta), color = Palette.accent)
                }
            }
        }
    }
}

@Composable
private fun RecentRidesSection(activities: List<ActivitySummary>) {
    Panel {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                text = stringResource(R.string.dashboard_recent_rides_title),
                style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            activities.forEach { act ->
                RecentRideRow(activity = act)
            }
        }
    }
}

@Composable
private fun RecentRideRow(activity: ActivitySummary) {
    val dateStr = activity.startTime?.take(10) ?: "—"
    val durationMin = activity.durationS?.let { (it / 60).toInt() } ?: 0
    val distStr = activity.distanceM?.let {
        val distKm = it / 1000.0
        String.format(Locale.US, "%.1f %s", distKm, stringResource(R.string.unit_km))
    } ?: ""
    val npVal = activity.np?.let { "${it.toInt()} ${stringResource(R.string.unit_watts_np)}" } ?: ""
    val tssVal = activity.tss?.let { "${it.toInt()} ${stringResource(R.string.unit_tss)}" } ?: ""
    val durationStr = "$durationMin ${stringResource(R.string.unit_min)}"

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Palette.surfaceInset, RoundedCornerShape(4.dp))
            .padding(horizontal = 10.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(
                text = dateStr,
                style = TextStyle(fontSize = 13.sp, fontWeight = FontWeight.Medium, color = Palette.textBright),
            )
            Text(
                text = "$durationStr${if (distStr.isNotEmpty()) " · $distStr" else ""}",
                style = TextStyle(fontSize = 11.sp, color = Palette.muted),
            )
        }
        Text(
            text = "$npVal${if (npVal.isNotEmpty() && tssVal.isNotEmpty()) " · " else ""}$tssVal",
            style = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = Palette.accent),
        )
    }
}
