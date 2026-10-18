package com.wattracker.android.screens.activities

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
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
import com.wattracker.android.cloud.ReadModel
import com.wattracker.android.json.JsonValue
import com.wattracker.android.shell.Panel
import com.wattracker.android.ui.theme.Palette
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt
import kotlinx.coroutines.CancellationException

/**
 * One ride's detail: the summary, the zone times, and the "Ride graphs"
 * chart. Shared by the two-pane layout (the detail beside the list) and the
 * pushed screen in a phone portrait.
 *
 * Mirrors the iOS `ActivityDetailScreen`: the detail and the streams go out
 * in parallel and fail independently; a streams 404 (or an empty payload) is
 * the rider's "No recorded streams", not an error. The chart renders the
 * published 1,500-point downsample as-is: no raw requests, no resampling,
 * and no client-side "fixing" of correction ranges, which the server has
 * already applied.
 *
 * The pane's view model is one per screen scope (keyed on the backend), not
 * one per ride: in the two-pane layout a ride's detail and streams must not
 * pile up as the rider clicks through the list. The pane reloads on resume
 * (a waking cloud's read re-runs) and releases its ride data on stop, and it
 * pulls to refresh -- the detail's waking notice is otherwise a dead end
 * until the waking gate expires, so the pull is the rider's way of lifting
 * it (the list and the dashboard pull the same way; iOS has no pull on its
 * detail, which sits under the list's).
 */
@Composable
fun RideDetailPane(
    activityId: Int,
    readModel: ReadModel,
    sourceName: String,
    onBack: (() -> Unit)? = null,
    modifier: Modifier = Modifier,
) {
    val factory = remember(readModel) {
        object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                return RideDetailViewModel(readModel) as T
            }
        }
    }
    val viewModel: RideDetailViewModel = viewModel(key = "detail-$sourceName", factory = factory)
    val uiState by viewModel.uiState.collectAsState()
    LaunchedEffect(activityId) { viewModel.load(activityId) }

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, viewModel) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                // A resume re-runs the read: a waking cloud's first read
                // failed, and the next resume is the retry the rider gets
                // without touching anything.
                Lifecycle.Event.ON_RESUME -> viewModel.reload()
                // A stopped pane keeps no ride data; the session's bounded
                // cache still does.
                Lifecycle.Event.ON_STOP -> viewModel.release()
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
        }
    }

    val shown = uiState.shownSummary
    val zones: JsonValue? = uiState.detail?.zones

    PullToRefreshBox(
        isRefreshing = uiState.isRefreshing,
        onRefresh = { viewModel.pullToRefresh() },
        modifier = modifier
            .fillMaxSize()
            .background(Palette.bg),
    ) {
        Column(
            modifier = Modifier
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
        ) {
            if (onBack != null) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = onBack) {
                        Icon(
                            imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = stringResource(R.string.back),
                            tint = Palette.muted,
                        )
                    }
                    Text(
                        text = stringResource(R.string.ride_detail_title),
                        style = TextStyle(fontSize = 18.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                    )
                }
            }

            when {
                // Opening or resuming: the pairing state has not been read
                // yet, so neither panel below is known, and showing one would
                // flash "No backend paired" over the ride a frame later.
                uiState.isStarting -> Unit

                uiState.deviceState == CloudSession.DeviceState.removed -> {
                    DeviceStatePanel(
                        title = stringResource(R.string.activities_removed_title),
                        note = stringResource(R.string.activities_removed_note),
                    )
                }

                !uiState.isPaired -> {
                    DeviceStatePanel(
                        title = stringResource(R.string.activities_unpaired_title),
                        note = stringResource(R.string.activities_unpaired_note),
                    )
                }

                else -> {
                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Text(
                            text = RideFormatting.date(shown?.startTime) ?: stringResource(R.string.ride_unknown_date),
                            style = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                        )
                        Text(
                            text = stringResource(R.string.activity_detail_subtitle),
                            style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                        )
                        if (uiState.isWaking) {
                            WakingNotice()
                        }
                    }
                    Spacer(modifier = Modifier.height(12.dp))

                    SummaryPanel(summary = shown)

                    if (zones != null) {
                        ZoneSection(zones = zones)
                    }

                    when {
                        // The chart comes first: each read fails on its own,
                        // so a detail that woke badly or was rate limited
                        // must not cover streams that loaded fine.
                        uiState.streams != null -> {
                            RideGraphsChart(
                                streams = uiState.streams!!,
                                durationSeconds = shown?.durationS,
                            )
                        }

                        uiState.isWaking -> {
                            // A static notice, not a spinner: the pane's
                            // resume re-runs the read, and a pull lifts the
                            // waking gate so the retry goes out now.
                            WakingCard()
                        }

                        uiState.throttledRetrySeconds != null -> {
                            ThrottledPanel(uiState.throttledRetrySeconds!!)
                        }

                        uiState.isLoading -> {
                            Panel {
                                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                                    CircularProgressIndicator(
                                        modifier = Modifier.size(18.dp),
                                        color = Palette.accent,
                                        strokeWidth = 2.dp,
                                    )
                                    Text(
                                        text = stringResource(R.string.activities_loading_streams),
                                        style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                                    )
                                }
                            }
                        }

                        uiState.noStreams -> {
                            Panel {
                                Text(
                                    text = stringResource(R.string.activities_no_streams),
                                    style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                                )
                            }
                        }

                        uiState.streamsError != null -> {
                            Panel {
                                Text(
                                    text = uiState.streamsError!!,
                                    style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                                )
                            }
                        }
                    }

                    uiState.detailError?.let {
                        Text(
                            text = it,
                            style = TextStyle(fontSize = 12.sp, color = Palette.alert),
                        )
                    }
                }
            }
        }
    }
}

/** The removed/unpaired state: the same honest panels the list uses. */
@Composable
private fun DeviceStatePanel(title: String, note: String) {
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
        }
    }
}

@Composable
private fun ThrottledPanel(retrySeconds: Int) {
    Panel {
        Text(
            text = stringResource(R.string.dashboard_throttled_notice, retrySeconds),
            style = TextStyle(fontSize = 14.sp, color = Palette.muted),
        )
    }
}

/** The summary metrics, adaptive across the available width. */
@Composable
private fun SummaryPanel(summary: com.wattracker.android.cloud.ActivitySummary?) {
    Panel {
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Metric(label = stringResource(R.string.metric_duration), value = RideFormatting.duration(summary?.durationS))
            Metric(label = stringResource(R.string.metric_distance), value = RideFormatting.distance(summary?.distanceM))
            Metric(label = stringResource(R.string.metric_avg_power), value = RideFormatting.watts(summary?.avgPower))
            Metric(label = stringResource(R.string.metric_np), value = RideFormatting.watts(summary?.np))
            Metric(label = stringResource(R.string.metric_if), value = RideFormatting.decimal(summary?.intensityFactor, 2))
            Metric(label = stringResource(R.string.metric_tss), value = RideFormatting.decimal(summary?.tss, 0))
            Metric(label = stringResource(R.string.metric_rpe), value = RideFormatting.decimal(summary?.rpe, 0))
            summary?.avgHr?.let {
                Metric(label = stringResource(R.string.metric_avg_hr), value = "${it.roundToInt()} bpm")
            }
        }
    }
}

@Composable
private fun Metric(label: String, value: String) {
    Column(verticalArrangement = Arrangement.spacedBy(1.dp)) {
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

/** The zone time panels, one per block the server published. */
@Composable
private fun ZoneSection(zones: JsonValue?) {
    val groups = ZoneGroup.extract(zones)
    groups.forEach { group ->
        Panel {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(
                    text = group.title,
                    style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
                group.rows.forEach { row ->
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            text = row.label,
                            style = TextStyle(fontSize = 14.sp, color = Palette.text),
                        )
                        Spacer(modifier = Modifier.weight(1f))
                        Text(
                            text = row.durationText,
                            style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                        )
                        Text(
                            text = "${row.percent.roundToInt()}%",
                            style = TextStyle(
                                fontSize = 14.sp,
                                color = Palette.text,
                                textAlign = TextAlign.End,
                            ),
                            modifier = Modifier
                                .padding(start = 10.dp)
                                .width(48.dp),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun WakingNotice() {
    Text(
        text = stringResource(R.string.dashboard_waking_title),
        style = TextStyle(fontSize = 12.sp, color = Palette.muted),
    )
}

/** The no-data waking state: a static notice, not a spinner that never resolves. */
@Composable
private fun WakingCard() {
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
 * The pushed ride detail in a phone portrait: the [RideDetailPane] with a
 * back arrow, where the header's summary comes from the cached list (the
 * pane's view model reads it) because the navigation carries only the id.
 */
@Composable
fun ActivityDetailScreen(
    activityId: Int,
    onBack: () -> Unit,
) {
    val context = LocalContext.current
    val activeSource by WatTrackerApp.dataSourceFlow.collectAsState()

    // The read model waits on the app's blocking setup, off the main thread;
    // a setup failure is shown, not thrown into composition.
    val readModelResult by produceState<Result<ReadModel>?>(initialValue = null, activeSource) {
        value = try {
            Result.success(WatTrackerApp.readModel(context))
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
    val readModel = readModelResult?.getOrNull()
    if (readModel == null) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(Palette.bg)
                .padding(16.dp),
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

    RideDetailPane(
        activityId = activityId,
        readModel = readModel,
        sourceName = activeSource.name,
        onBack = onBack,
    )
}

/** The "updated" line: the same absolute stamp the dashboard's cache banner uses. */
internal fun updatedAt(epochMillis: Long): String =
    if (epochMillis > 0) {
        SimpleDateFormat("MMM d, HH:mm", Locale.getDefault()).format(Date(epochMillis))
    } else {
        "—"
    }
