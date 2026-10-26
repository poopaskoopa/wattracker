package com.wattracker.android.screens

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
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
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
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
import com.wattracker.android.screens.activities.ChartTicks
import com.wattracker.android.screens.activities.updatedAt
import com.wattracker.android.screens.volume.VolumeData
import com.wattracker.android.screens.volume.VolumeFormat
import com.wattracker.android.screens.volume.VolumeMetric
import com.wattracker.android.screens.volume.VolumeRange
import com.wattracker.android.screens.volume.VolumeUiState
import com.wattracker.android.screens.volume.VolumeViewModel
import com.wattracker.android.shell.Panel
import com.wattracker.android.shell.ScrollableScreenScaffold
import com.wattracker.android.ui.theme.Palette
import kotlinx.coroutines.CancellationException
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * Volume (#198): weekly hours, TSS, distance and calories -- bars for a
 * window, totals for the same window, and the same totals across the three
 * windows.
 *
 * A column screen on every idiom: the chart scales with the width it gets,
 * so landscape phones and tablets see the same layout wider -- no second
 * pane to split a single chart across. The weeks themselves are the
 * desktop's Monday buckets, read as published and never re-bucketed here.
 */
@Composable
fun VolumeScreen(
    onNavigateToSettings: (() -> Unit)? = null,
) {
    val context = LocalContext.current
    val activeSource by WatTrackerApp.dataSourceFlow.collectAsState()

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
            title = stringResource(R.string.destination_volume),
            subtitle = stringResource(R.string.screen_volume_subtitle),
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
                return VolumeViewModel(currentReadModel) as T
            }
        }
    }
    val viewModel: VolumeViewModel = viewModel(key = activeSource.name, factory = factory)
    val uiState by viewModel.uiState.collectAsState()

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, viewModel) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) viewModel.refresh()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    // The scaffold paints the title and subtitle; the content below adds
    // only the state chrome (offline badge, waking, throttle) and then the
    // chart, tiles and windows -- no second "Volume" heading.
    ScrollableScreenScaffold(
        title = stringResource(R.string.destination_volume),
        subtitle = stringResource(R.string.screen_volume_subtitle),
        isRefreshing = uiState.isRefreshing,
        onRefresh = { viewModel.pullToRefresh() },
    ) {
        if (uiState.isWaking) {
            Text(
                text = stringResource(R.string.dashboard_waking_title),
                style = TextStyle(fontSize = 12.sp, color = Palette.muted),
            )
        }
        val throttled = uiState.throttledRetrySeconds
        if (throttled != null) {
            Text(
                text = stringResource(R.string.dashboard_throttled_notice, throttled),
                style = TextStyle(fontSize = 12.sp, color = Palette.alert),
            )
        }

        val data = uiState.data
        if (data != null) {
            VolumeContent(data = data, uiState = uiState, viewModel = viewModel)
        } else {
            VolumeStates(uiState = uiState, onNavigateToSettings = onNavigateToSettings)
        }
    }
}

// MARK: - Content

@Composable
private fun VolumeContent(
    data: VolumeData,
    uiState: VolumeUiState,
    viewModel: VolumeViewModel,
) {
    val weeks = remember(data, uiState.selectedRange) { data.weeksFor(uiState.selectedRange, LocalDate.now()) }
    val summary = remember(data, uiState.selectedRange) { data.summaryFor(uiState.selectedRange, LocalDate.now()) }

    if (data.source == CloudSnapshot.Source.cache) {
        Text(
            text = stringResource(R.string.dashboard_cached_data_banner, updatedAt(data.asOf)),
            style = TextStyle(fontSize = 12.sp, color = Palette.accent),
            modifier = Modifier.padding(horizontal = 4.dp),
        )
    }

    Panel(modifier = Modifier.fillMaxWidth()) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            // Metric and window: two segmented pickers, stacked when the
            // row cannot hold them (a phone in portrait).
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                SegmentedPicker(
                    label = stringResource(R.string.volume_metric_label),
                    options = VolumeMetric.entries.map { stringResource(it.titleRes) },
                    selectedIndex = uiState.selectedMetric.ordinal,
                    onSelect = { viewModel.selectMetric(VolumeMetric.entries[it]) },
                )
                SegmentedPicker(
                    label = null,
                    options = VolumeRange.entries.map { stringResource(it.shortTitleRes) },
                    selectedIndex = uiState.selectedRange.ordinal,
                    onSelect = { viewModel.selectRange(VolumeRange.entries[it]) },
                )
            }

            if (weeks.isEmpty()) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(220.dp)
                        .background(Palette.surfaceInset, RoundedCornerShape(6.dp)),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = stringResource(R.string.volume_no_weeks),
                        style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                    )
                }
            } else {
                VolumeBarChart(
                    weeks = weeks,
                    metric = uiState.selectedMetric,
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(220.dp),
                )
            }
        }
    }

    // Totals for the window, with the change against the previous one.
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(
            modifier = Modifier.padding(horizontal = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = stringResource(R.string.volume_totals_title, stringResource(uiState.selectedRange.titleRes)),
                style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            Spacer(modifier = Modifier.weight(1f))
            val change = data.change(uiState.selectedMetric, uiState.selectedRange, LocalDate.now())
            if (change != null) {
                Text(
                    text = (if (change >= 0) "\u25b2 " else "\u25bc ") + VolumeFormat.percent(change),
                    style = TextStyle(
                        fontSize = 12.sp,
                        fontWeight = FontWeight.Bold,
                        color = if (change >= 0) Palette.ok else Palette.alert,
                    ),
                )
            }
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            VolumeMetric.entries.forEach { metric ->
                VolumeSummaryTile(
                    title = stringResource(metric.titleRes),
                    value = VolumeFormat.value(summary.valueFor(metric), metric),
                    selected = metric == uiState.selectedMetric,
                    modifier = Modifier.widthIn(min = 124.dp).weight(1f),
                )
            }
        }
    }

    // The same metric across all three windows.
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(
            text = stringResource(R.string.volume_period_title, stringResource(uiState.selectedMetric.titleRes)),
            style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            modifier = Modifier.padding(horizontal = 4.dp),
        )
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            VolumeRange.entries.forEach { range ->
                val value = data.summaryFor(range, LocalDate.now()).valueFor(uiState.selectedMetric)
                Panel(modifier = Modifier.widthIn(min = 124.dp).weight(1f)) {
                    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(
                            text = stringResource(range.titleRes),
                            style = TextStyle(fontSize = 11.sp, color = Palette.muted),
                        )
                        Text(
                            text = VolumeFormat.value(value, uiState.selectedMetric),
                            style = TextStyle(
                                fontSize = 18.sp,
                                fontWeight = FontWeight.Bold,
                                fontFamily = FontFamily.Monospace,
                                color = Palette.textBright,
                            ),
                            maxLines = 1,
                        )
                    }
                }
            }
        }
    }
}

/** The dashboard window selector's look: a recessed track, a raised selected pill. */
@Composable
private fun SegmentedPicker(
    label: String?,
    options: List<String>,
    selectedIndex: Int,
    onSelect: (Int) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        if (label != null) {
            Text(text = label, style = TextStyle(fontSize = 12.sp, color = Palette.muted))
        }
        Row(
            modifier = Modifier
                .background(Palette.surfaceInset, RoundedCornerShape(6.dp))
                .border(1.dp, Palette.surfaceBorder, RoundedCornerShape(6.dp))
                .padding(2.dp),
        ) {
            options.forEachIndexed { index, option ->
                val isSelected = index == selectedIndex
                Box(
                    modifier = Modifier
                        .background(
                            if (isSelected) Palette.surface2 else Color.Transparent,
                            RoundedCornerShape(4.dp),
                        )
                        .clickable { onSelect(index) }
                        .padding(horizontal = 10.dp, vertical = 5.dp),
                ) {
                    Text(
                        text = option,
                        style = TextStyle(
                            fontSize = 12.sp,
                            fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                            color = if (isSelected) Palette.textBright else Palette.muted,
                        ),
                    )
                }
            }
        }
    }
}

@Composable
private fun VolumeSummaryTile(title: String, value: String, selected: Boolean, modifier: Modifier = Modifier) {
    Panel(modifier = modifier) {
        Column(
            modifier = Modifier.height(46.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(
                text = title,
                style = TextStyle(fontSize = 11.sp, color = if (selected) Palette.accent else Palette.muted),
            )
            Text(
                text = value,
                style = TextStyle(
                    fontSize = 17.sp,
                    fontWeight = FontWeight.Bold,
                    fontFamily = FontFamily.Monospace,
                    color = Palette.textBright,
                ),
                maxLines = 1,
            )
        }
    }
}

/**
 * The bar chart: one bar per published week (gaps filled with zero bars, so
 * a rest week sits in the spacing rather than collapsing the weeks around
 * it), the window's weekly average dashed across, and the y-axis on the
 * same 1/2/5 nice ticks every other chart in the app uses.
 *
 * The bars are iOS's volume blue, not the accent gold: iOS deliberately
 * keeps the accent for chrome and paints this chart in the desktop volume
 * page's blue (its `Palette.hex(0x3987e5)`), and the parity rule for this
 * screen is "look like iOS".
 */
@Composable
private fun VolumeBarChart(
    weeks: List<com.wattracker.android.cloud.VolumeWeek>,
    metric: VolumeMetric,
    modifier: Modifier = Modifier,
) {
    val textMeasurer = rememberTextMeasurer()
    val labelFormatter = remember { DateTimeFormatter.ofPattern("MMM d", Locale.getDefault()) }
    val averageLabel = stringResource(
        R.string.volume_avg_label,
        VolumeFormat.value(
            weeks.map { metric.valueFrom(it) }.average().takeIf { it.isFinite() } ?: 0.0,
            metric,
        ),
    )

    Canvas(modifier) {
        val barColor = Color(0xFF3987E5)
        val padLeft = 40.dp.toPx()
        val padRight = 6.dp.toPx()
        val padTop = 12.dp.toPx()
        val padBottom = 20.dp.toPx()
        val plotLeft = padLeft
        val plotRight = size.width - padRight
        val plotTop = padTop
        val plotBottom = size.height - padBottom

        val values = weeks.map { metric.valueFrom(it) }
        val maxValue = (values.maxOrNull() ?: 0.0).let { if (it <= 0) 1.0 else it * 1.05 }
        val scale = ChartTicks.nice(0.0, maxValue, 4)

        fun yPos(value: Double): Float =
            plotBottom - (value / scale.upper).toFloat() * (plotBottom - plotTop)

        // Grid and y labels.
        for (tick in scale.ticks) {
            val y = yPos(tick)
            drawLine(
                color = Palette.surfaceBorder,
                start = Offset(plotLeft, y),
                end = Offset(plotRight, y),
                strokeWidth = 1f,
            )
            val label = textMeasurer.measure(
                text = "${tick.toInt()}",
                style = TextStyle(fontSize = 9.sp, color = Palette.muted),
            )
            drawText(textLayoutResult = label, topLeft = Offset(4.dp.toPx(), y - label.size.height / 2f))
        }

        // Bars: one slot per week, two thirds of it bar.
        val count = weeks.size
        val slot = (plotRight - plotLeft) / count
        val barWidth = slot * 0.62f
        values.forEachIndexed { index, value ->
            if (value <= 0) return@forEachIndexed
            val center = plotLeft + slot * (index + 0.5f)
            val top = yPos(value)
            drawRoundRect(
                color = barColor,
                topLeft = Offset(center - barWidth / 2f, top),
                size = androidx.compose.ui.geometry.Size(barWidth, plotBottom - top),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(3f, 3f),
            )
        }

        // The window's weekly average, dashed.
        val average = values.average().takeIf { it.isFinite() } ?: 0.0
        if (count > 0 && average > 0) {
            val y = yPos(average)
            drawLine(
                color = Palette.accent,
                start = Offset(plotLeft, y),
                end = Offset(plotRight, y),
                strokeWidth = 1f,
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(10f, 8f)),
            )
            val label = textMeasurer.measure(
                averageLabel,
                style = TextStyle(fontSize = 9.sp, color = Palette.accent),
            )
            drawText(
                textLayoutResult = label,
                topLeft = Offset(plotRight - label.size.width, y - label.size.height - 2f),
            )
        }

        // Week-start labels, thinned so at most about six are drawn.
        val labelStep = maxOf(1, kotlin.math.ceil(count / 6.0).toInt())
        for (index in weeks.indices step labelStep) {
            val date = com.wattracker.android.cloud.isoDateOrNull(weeks[index].weekStart) ?: continue
            val label = textMeasurer.measure(
                text = labelFormatter.format(date),
                style = TextStyle(fontSize = 9.sp, color = Palette.muted),
            )
            val center = plotLeft + slot * (index + 0.5f)
            drawText(
                textLayoutResult = label,
                topLeft = Offset(center - label.size.width / 2f, plotBottom + 2.dp.toPx()),
            )
        }
    }
}

// MARK: - Honest states (when there is no chart to show)

/**
 * The states shown only when there are no weeks on screen: the
 * unpaired/removed cards, a failed load with no cache, the waking notice,
 * the first-load spinner, and "nothing published yet". A failed load is
 * never presented as an empty history.
 */
@Composable
private fun VolumeStates(
    uiState: VolumeUiState,
    onNavigateToSettings: (() -> Unit)?,
) {
    when {
        uiState.deviceState == CloudSession.DeviceState.removed -> VolumeStateCard(
            title = stringResource(R.string.volume_removed_title),
            note = stringResource(R.string.volume_removed_note),
            onNavigateToSettings = onNavigateToSettings,
        )

        !uiState.isPaired || uiState.deviceState == CloudSession.DeviceState.unpaired -> VolumeStateCard(
            title = stringResource(R.string.volume_unpaired_title),
            note = stringResource(R.string.volume_unpaired_note),
            onNavigateToSettings = onNavigateToSettings,
        )

        uiState.error != null -> Panel {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(
                    text = stringResource(R.string.volume_unavailable),
                    style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
                Text(
                    text = uiState.error,
                    style = TextStyle(fontSize = 13.sp, color = Palette.alert),
                )
            }
        }

        uiState.isWaking -> Panel {
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

        uiState.isStarting || uiState.isLoading -> Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(140.dp)
                .background(Palette.surfaceInset),
            contentAlignment = Alignment.Center,
        ) {
            CircularProgressIndicator(modifier = Modifier.size(24.dp), color = Palette.accent, strokeWidth = 2.dp)
        }

        else -> VolumeStateCard(
            title = stringResource(R.string.volume_no_data_title),
            note = stringResource(R.string.volume_no_data_note),
            onNavigateToSettings = null,
        )
    }
}

@Composable
private fun VolumeStateCard(
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
            Text(text = note, style = TextStyle(fontSize = 13.sp, color = Palette.muted))
            if (onNavigateToSettings != null) {
                OutlinedButton(onClick = onNavigateToSettings) {
                    Text(stringResource(R.string.dashboard_pair_cta))
                }
            }
        }
    }
}
