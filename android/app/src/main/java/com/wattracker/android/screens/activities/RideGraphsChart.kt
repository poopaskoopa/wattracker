package com.wattracker.android.screens.activities

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.wattracker.android.R
import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.shell.Panel
import com.wattracker.android.ui.theme.Palette
import kotlin.math.roundToInt

/**
 * The "Ride graphs" chart: one combined chart, not per-channel charts.
 *
 * Altitude (a faint filled background, no axis) is drawn behind power (its
 * own zero-based left axis, "W"), heart rate and cadence (one shared
 * zero-based right axis, "bpm / rpm"), over a true-minutes x-axis. The
 * model (series, axes, clock, ticks) is RideDisplay.kt's, pinned by the
 * ported iOS tests; this composable is the drawing half. Built to look like
 * the iOS `StreamCharts`; the desktop palette is not copied.
 */
@Composable
fun RideGraphsChart(
    streams: ActivityStreams,
    durationSeconds: Double?,
    modifier: Modifier = Modifier,
) {
    val series = remember(streams, durationSeconds) {
        CombinedChartSeries.build(streams, durationSeconds)
    }
    if (series.isEmpty()) return

    // The axis titles read off the visible series, but the axes themselves
    // are built from every series, so toggling the legend never rescales
    // the plot.
    val all = remember(streams) { StreamSeries.all(streams) }
    var hidden by remember { mutableStateOf(setOf<String>()) }
    val visible = series.filter { it.id !in hidden }
    val powerAxis = if (visible.any { it.id == "power" }) {
        CombinedChartSeries.axis(CombinedChartSeries.Side.power, all)
    } else {
        null
    }
    val sharedAxis = if (visible.any { it.id == "heart-rate" || it.id == "cadence" }) {
        CombinedChartSeries.axis(CombinedChartSeries.Side.shared, all)
    } else {
        null
    }
    val extent = CombinedChartSeries.extent(series)
    val xTicks = ChartTicks.minutes(extent)

    Panel(modifier = modifier) {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                text = stringResource(R.string.ride_graphs_title),
                style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            Row(modifier = Modifier.fillMaxWidth()) {
                Text(
                    text = powerAxis?.title ?: "",
                    style = TextStyle(fontSize = 10.sp, color = Palette.accent),
                )
                Spacer(modifier = Modifier.weight(1f))
                Text(
                    text = sharedAxis?.title ?: "",
                    style = TextStyle(fontSize = 10.sp, color = Palette.muted),
                )
            }
            RideGraphsCanvas(
                series = series,
                hidden = hidden,
                powerAxis = powerAxis,
                sharedAxis = sharedAxis,
                xTicks = xTicks,
                extent = extent,
            )
            Text(
                text = stringResource(R.string.ride_graphs_caption),
                style = TextStyle(fontSize = 10.sp, color = Palette.muted),
            )
            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                series.forEach { s ->
                    val off = s.id in hidden
                    val dim = if (off) 0.4f else 1f
                    Row(
                        modifier = Modifier
                            .widthIn(min = 150.dp)
                            .clickable {
                                hidden = if (off) hidden - s.id else hidden + s.id
                            },
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        Box(
                            modifier = Modifier
                                .size(9.dp)
                                .background(s.color.copy(alpha = dim), CircleShape),
                        )
                        Text(
                            text = "${s.title} ${seriesRange(s)}",
                            style = TextStyle(fontSize = 12.sp, color = Palette.text.copy(alpha = dim)),
                        )
                    }
                }
            }
        }
    }
}

/** The legend range: the data's min-max for the background, 0-max for the axes. */
private fun seriesRange(s: CombinedChartSeries): String {
    val values = s.points.map { it.value }
    val lo = values.minOrNull() ?: return ""
    val hi = values.maxOrNull() ?: return ""
    val low = if (s.isBackground) lo else 0.0
    return "${low.roundToInt()}-${hi.roundToInt()}"
}

private val tickLabelStyle = TextStyle(fontSize = 10.sp, color = Palette.muted)

@Composable
private fun RideGraphsCanvas(
    series: List<CombinedChartSeries>,
    hidden: Set<String>,
    powerAxis: ChartAxis?,
    sharedAxis: ChartAxis?,
    xTicks: List<Double>,
    extent: Double,
) {
    val textMeasurer = rememberTextMeasurer()

    Canvas(
        modifier = Modifier
            .fillMaxWidth()
            .height(220.dp)
            // The canvas does not clip by default; a stray segment must not
            // paint over the title or the caption below.
            .clipToBounds(),
    ) {
        val width = size.width
        val height = size.height

        // The labels are measured in scope so the plot's padding fits them:
        // the left axis is absent when power is hidden, and so is the right
        // one, so a hidden axis costs no label width.
        val leftLabels = powerAxis?.ticks?.map { chartTickLabel(it) } ?: emptyList()
        val rightLabels = sharedAxis?.ticks?.map { chartTickLabel(it) } ?: emptyList()
        val bottomLabels = xTicks.map { chartTickLabel(it) }

        val leftPad = if (leftLabels.isNotEmpty()) {
            leftLabels.maxOf { textMeasurer.measure(it, tickLabelStyle).size.width.toFloat() } + 8.dp.toPx()
        } else {
            8.dp.toPx()
        }
        val rightPad = if (rightLabels.isNotEmpty()) {
            rightLabels.maxOf { textMeasurer.measure(it, tickLabelStyle).size.width.toFloat() } + 8.dp.toPx()
        } else {
            8.dp.toPx()
        }
        val topPad = 8.dp.toPx()
        val bottomPad = (bottomLabels.maxOfOrNull { textMeasurer.measure(it, tickLabelStyle).size.height.toFloat() } ?: 14f) +
            4.dp.toPx()

        val graphWidth = width - leftPad - rightPad
        val graphHeight = height - topPad - bottomPad
        if (graphWidth <= 0f || graphHeight <= 0f) return@Canvas

        // The domain is 0...ride length in minutes, never less than one
        // sample at 1 Hz.
        val domain = maxOf(extent, 1.0 / 60.0)
        fun xPos(minutes: Double): Float = leftPad + (minutes / domain).toFloat() * graphWidth
        fun yPos(scaled: Double): Float = topPad + (1.0 - scaled).toFloat() * graphHeight

        // Grid lines come from the power axis, or from the right axis when
        // power is hidden.
        val gridAxis = powerAxis ?: sharedAxis
        gridAxis?.ticks?.forEach { tick ->
            val y = yPos((tick - gridAxis.lower) / (gridAxis.upper - gridAxis.lower))
            drawLine(
                color = Palette.surfaceBorder,
                start = Offset(leftPad, y),
                end = Offset(width - rightPad, y),
                strokeWidth = 1f,
            )
        }
        xTicks.forEach { tick ->
            val x = xPos(tick)
            drawLine(
                color = Palette.surfaceBorder,
                start = Offset(x, topPad),
                end = Offset(x, height - bottomPad),
                strokeWidth = 1f,
            )
        }

        // The series, altitude behind; hidden ones are skipped entirely.
        // Lines are linear, no markers; a one-point series has no segment.
        series.forEach { s ->
            if (s.id in hidden) return@forEach
            val points = s.points
            if (points.size < 2) return@forEach
            val path = Path()
            points.forEachIndexed { i, p ->
                val x = xPos(p.minutes)
                val y = yPos(p.scaled)
                if (i == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            if (s.isBackground) {
                // A faint filled area down to the plot floor, plus the 1pt line.
                val fill = Path().apply {
                    addPath(path)
                    lineTo(xPos(points.last().minutes), height - bottomPad)
                    lineTo(xPos(points.first().minutes), height - bottomPad)
                    close()
                }
                drawPath(
                    path = fill,
                    color = s.color.copy(alpha = CombinedChartSeries.altitudeFillOpacity),
                )
                drawPath(
                    path = path,
                    color = s.color.copy(alpha = CombinedChartSeries.altitudeLineOpacity),
                    style = Stroke(width = 1f),
                )
            } else {
                drawPath(
                    path = path,
                    color = s.color,
                    style = Stroke(width = 1.5.dp.toPx()),
                )
            }
        }

        // The tick labels, in the muted tick colour.
        if (powerAxis != null) {
            powerAxis.ticks.zip(leftLabels).forEach { (tick, label) ->
                val y = yPos((tick - powerAxis.lower) / (powerAxis.upper - powerAxis.lower))
                val measured = textMeasurer.measure(label, tickLabelStyle)
                drawText(
                    textLayoutResult = measured,
                    topLeft = Offset(
                        leftPad - 4.dp.toPx() - measured.size.width.toFloat(),
                        y - measured.size.height / 2f,
                    ),
                )
            }
        }
        if (sharedAxis != null) {
            sharedAxis.ticks.zip(rightLabels).forEach { (tick, label) ->
                val y = yPos((tick - sharedAxis.lower) / (sharedAxis.upper - sharedAxis.lower))
                val measured = textMeasurer.measure(label, tickLabelStyle)
                drawText(
                    textLayoutResult = measured,
                    topLeft = Offset(width - rightPad + 4.dp.toPx(), y - measured.size.height / 2f),
                )
            }
        }
        xTicks.zip(bottomLabels).forEach { (tick, label) ->
            val measured = textMeasurer.measure(label, tickLabelStyle)
            drawText(
                textLayoutResult = measured,
                topLeft = Offset(xPos(tick) - measured.size.width / 2f, height - bottomPad + 2.dp.toPx()),
            )
        }
    }
}
