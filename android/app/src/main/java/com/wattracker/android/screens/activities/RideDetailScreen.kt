package com.wattracker.android.screens.activities

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.compose.viewModel
import com.wattracker.android.R
import com.wattracker.android.WatTrackerApp
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.ReadModel
import com.wattracker.android.json.JsonValue
import com.wattracker.android.shell.Panel
import com.wattracker.android.ui.theme.Palette
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.CancellationException
import kotlin.math.roundToInt

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
 */
@Composable
fun RideDetailPane(
    activityId: Int,
    readModel: ReadModel,
    /** The list's summary for the header, when the two-pane layout has it. */
    summary: ActivitySummary? = null,
    onBack: (() -> Unit)? = null,
    modifier: Modifier = Modifier,
) {
    val factory = remember(readModel, activityId) {
        object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                return RideDetailViewModel(readModel, activityId) as T
            }
        }
    }
    val viewModel: RideDetailViewModel = viewModel(key = "detail-$activityId", factory = factory)
    val uiState by viewModel.uiState.collectAsState()
    LaunchedEffect(activityId) { viewModel.load() }

    val shown = uiState.shownSummary ?: summary
    val zones: JsonValue? = uiState.detail?.zones

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(Palette.bg)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
    ) {
        if (onBack != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onBack) {
                    Icon(
                        imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                        contentDescription = stringResource(R.string.ride_detail_title),
                        tint = Palette.muted,
                    )
                }
                Text(
                    text = stringResource(R.string.ride_detail_title),
                    style = TextStyle(fontSize = 18.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
            }
        }

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
            uiState.isWaking -> {
                // A static notice, not a spinner: the server is waking, and a
                // pull or the next resume re-runs the read.
                WakingCard()
            }

            uiState.streams != null -> {
                RideGraphsChart(
                    streams = uiState.streams!!,
                    durationSeconds = shown?.durationS,
                )
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

/** The summary metrics, adaptive across the available width. */
@Composable
private fun SummaryPanel(summary: ActivitySummary?) {
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
                            style = TextStyle(fontSize = 14.sp, color = Palette.text),
                            modifier = Modifier.width(42.dp),
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
    val context = androidx.compose.ui.platform.LocalContext.current
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
        Column(
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
