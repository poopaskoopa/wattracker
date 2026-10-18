package com.wattracker.android.screens.activities

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
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
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.wattracker.android.R
import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.shell.Panel
import com.wattracker.android.ui.theme.Palette
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * The "Ride graphs" chart: one combined chart, not per-channel charts.
 *
 * Altitude (a faint filled background, no axis) is drawn behind power (its
 * own zero-based left axis, "W"), heart rate and cadence (one shared
 * zero-based right axis, "bpm / rpm"), over a true-minutes x-axis. The
 * model (series, axes, clock, ticks) is RideDisplay.kt's, pinned by the
 * ported iOS tests; this composable is the drawing half.
 *
 * The drawing is done once per change, not once per frame: the label
 * layouts and the series paths are built in composition (remembered on
 * their inputs and the canvas size) and the canvas only strokes them.
 */
@Composable
fun RideGraphsChart(
    streams: ActivityStreams,
    durationSeconds: Double?,
    modifier: Modifier = Modifier,
) {
    val all = remember(streams) { StreamSeries.all(streams) }
    val series = remember(all, durationSeconds) {
        CombinedChartSeries.buildFrom(all, streams.streams, durationSeconds)
    }
    if (series.isEmpty()) return

    // The legend's hidden set is keyed on the ride's streams: a new ride
    // starts with nothing hidden.
    var hidden by remember(streams) { mutableStateOf(setOf<String>()) }
    val visible = series.filter { it.id !in hidden }

    // The axis titles read off the visible series, but the axes themselves
    // are built from every series, so toggling the legend never rescales
    // the plot.
    val powerAxis = remember(series, hidden, all) {
        if (visible.any { it.id == "power" }) {
            CombinedChartSeries.axis(CombinedChartSeries.Side.power, all)
        } else {
            null
        }
    }
    val sharedAxis = remember(series, hidden, all) {
        if (visible.any { it.id == "heart-rate" || it.id == "cadence" }) {
            CombinedChartSeries.axis(CombinedChartSeries.Side.shared, all)
        } else {
            null
        }
    }
    val extent = remember(series) { CombinedChartSeries.extent(series) }
    val xTicks = remember(extent) { ChartTicks.minutes(extent) }

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

private data class RenderedSeries(
    val color: androidx.compose.ui.graphics.Color,
    val isBackground: Boolean,
    val linePath: Path,
    val fillPath: Path?,
)

/**
 * The plot geometry: paddings that fit the labels, the tick positions, and
 * the series paths. Built in composition, remembered on its inputs and the
 * canvas size, so a redraw strokes ready paths instead of rebuilding
 * 1,500-point ones.
 */
private class Plot(
    val leftPad: Float,
    val rightPad: Float,
    val topPad: Float,
    val bottomPad: Float,
    val gridYs: List<Float>,
    val gridXs: List<Float>,
    val leftLabelYs: List<Float>,
    val rightLabelYs: List<Float>,
    val bottomLabelXs: List<Float>,
    val renderedSeries: List<RenderedSeries>,
) {
    companion object {
        fun resolve(
            width: Float,
            height: Float,
            series: List<CombinedChartSeries>,
            hidden: Set<String>,
            powerAxis: ChartAxis?,
            sharedAxis: ChartAxis?,
            xTicks: List<Double>,
            extent: Double,
            leftResults: List<TextLayoutResult>,
            rightResults: List<TextLayoutResult>,
            bottomResults: List<TextLayoutResult>,
            density: Density,
        ): Plot {
            val leftPad = if (leftResults.isNotEmpty()) {
                leftResults.maxOf { it.size.width.toFloat() } + with(density) { 8.dp.toPx() }
            } else {
                with(density) { 8.dp.toPx() }
            }
            val rightPad = if (rightResults.isNotEmpty()) {
                rightResults.maxOf { it.size.width.toFloat() } + with(density) { 8.dp.toPx() }
            } else {
                with(density) { 8.dp.toPx() }
            }
            val topPad = with(density) { 8.dp.toPx() }
            val bottomPad = (bottomResults.maxOfOrNull { it.size.height.toFloat() } ?: 14f) +
                with(density) { 4.dp.toPx() }

            val graphWidth = width - leftPad - rightPad
            val graphHeight = height - topPad - bottomPad
            if (graphWidth <= 0f || graphHeight <= 0f) {
                return Plot(leftPad, rightPad, topPad, bottomPad, emptyList(), emptyList(),
                    emptyList(), emptyList(), emptyList(), emptyList())
            }

            val domain = max(extent, 1.0 / 60.0)
            fun xPos(minutes: Double): Float = leftPad + (minutes / domain).toFloat() * graphWidth
            fun yPos(scaled: Double): Float = topPad + (1.0 - scaled).toFloat() * graphHeight

            val gridAxis = powerAxis ?: sharedAxis
            val gridYs = gridAxis?.ticks?.map {
                yPos((it - gridAxis.lower) / (gridAxis.upper - gridAxis.lower))
            } ?: emptyList()
            val gridXs = xTicks.map { xPos(it) }
            val leftLabelYs = powerAxis?.ticks?.map {
                yPos((it - powerAxis.lower) / (powerAxis.upper - powerAxis.lower))
            } ?: emptyList()
            val rightLabelYs = sharedAxis?.ticks?.map {
                yPos((it - sharedAxis.lower) / (sharedAxis.upper - sharedAxis.lower))
            } ?: emptyList()
            val bottomLabelXs = xTicks.map { xPos(it) }

            val rendered = mutableListOf<RenderedSeries>()
            for (s in series) {
                if (s.id in hidden) continue
                val points = s.points
                if (points.size < 2) continue
                val linePath = Path()
                points.forEachIndexed { i, p ->
                    val x = xPos(p.minutes)
                    val y = yPos(p.scaled)
                    if (i == 0) linePath.moveTo(x, y) else linePath.lineTo(x, y)
                }
                var fillPath: Path? = null
                if (s.isBackground) {
                    fillPath = Path().apply {
                        addPath(linePath)
                        lineTo(xPos(points.last().minutes), height - bottomPad)
                        lineTo(xPos(points.first().minutes), height - bottomPad)
                        close()
                    }
                }
                rendered.add(RenderedSeries(s.color, s.isBackground, linePath, fillPath))
            }

            return Plot(
                leftPad, rightPad, topPad, bottomPad,
                gridYs, gridXs, leftLabelYs, rightLabelYs, bottomLabelXs,
                rendered,
            )
        }
    }
}

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
    val density = LocalDensity.current

    val leftResults = remember(powerAxis, textMeasurer) {
        powerAxis?.ticks?.map { textMeasurer.measure(chartTickLabel(it), tickLabelStyle) } ?: emptyList()
    }
    val rightResults = remember(sharedAxis, textMeasurer) {
        sharedAxis?.ticks?.map { textMeasurer.measure(chartTickLabel(it), tickLabelStyle) } ?: emptyList()
    }
    val bottomResults = remember(xTicks, textMeasurer) {
        xTicks.map { textMeasurer.measure(chartTickLabel(it), tickLabelStyle) }
    }

    BoxWithConstraints(
        modifier = Modifier
            .fillMaxWidth()
            .height(220.dp) // the spec's chart height (docs/mobile-parity.md)
            .background(Palette.surfaceInset)
            .clipToBounds(),
    ) {
        val widthPx = with(density) { maxWidth.toPx() }
        val heightPx = with(density) { maxHeight.toPx() }

        val plot = remember(
            widthPx, heightPx, series, hidden, powerAxis, sharedAxis, xTicks, extent,
            leftResults, rightResults, bottomResults, density,
        ) {
            Plot.resolve(
                widthPx, heightPx, series, hidden, powerAxis, sharedAxis, xTicks, extent,
                leftResults, rightResults, bottomResults, density,
            )
        }

        Canvas(modifier = Modifier.fillMaxSize()) {
            // Draw grid lines
            plot.gridYs.forEach { y ->
                drawLine(
                    color = Palette.surfaceBorder,
                    start = Offset(plot.leftPad, y),
                    end = Offset(size.width - plot.rightPad, y),
                    strokeWidth = 1.dp.toPx(),
                )
            }
            plot.gridXs.forEach { x ->
                drawLine(
                    color = Palette.surfaceBorder,
                    start = Offset(x, plot.topPad),
                    end = Offset(x, size.height - plot.bottomPad),
                    strokeWidth = 1.dp.toPx(),
                )
            }

            // Draw background fills
            plot.renderedSeries.forEach { rs ->
                if (rs.fillPath != null) {
                    drawPath(
                        path = rs.fillPath,
                        color = rs.color.copy(alpha = CombinedChartSeries.altitudeFillOpacity),
                    )
                }
            }

            // Draw series lines at the spec's widths: 1.5 pt on the data
            // lines, 1 pt on the altitude context line (which also draws at
            // 0.5 alpha).
            plot.renderedSeries.forEach { rs ->
                val strokeWidth = if (rs.isBackground) 1.dp.toPx() else 1.5.dp.toPx()
                val opacity = if (rs.isBackground) CombinedChartSeries.altitudeLineOpacity else 1f
                drawPath(
                    path = rs.linePath,
                    color = rs.color.copy(alpha = opacity),
                    style = Stroke(width = strokeWidth),
                )
            }

            // Draw left axis labels
            leftResults.forEachIndexed { i, res ->
                if (i < plot.leftLabelYs.size) {
                    val y = plot.leftLabelYs[i] - res.size.height / 2f
                    drawText(
                        textLayoutResult = res,
                        topLeft = Offset(plot.leftPad - res.size.width - 4.dp.toPx(), y),
                    )
                }
            }

            // Draw right axis labels
            rightResults.forEachIndexed { i, res ->
                if (i < plot.rightLabelYs.size) {
                    val y = plot.rightLabelYs[i] - res.size.height / 2f
                    drawText(
                        textLayoutResult = res,
                        topLeft = Offset(size.width - plot.rightPad + 4.dp.toPx(), y),
                    )
                }
            }

            // Draw bottom axis labels
            bottomResults.forEachIndexed { i, res ->
                if (i < plot.bottomLabelXs.size) {
                    val x = plot.bottomLabelXs[i] - res.size.width / 2f
                    drawText(
                        textLayoutResult = res,
                        topLeft = Offset(x, size.height - plot.bottomPad + 2.dp.toPx()),
                    )
                }
            }
        }
    }
}
