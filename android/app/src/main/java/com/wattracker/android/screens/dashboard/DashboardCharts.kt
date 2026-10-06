package com.wattracker.android.screens.dashboard

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.wattracker.android.R
import com.wattracker.android.cloud.CurvePoint
import com.wattracker.android.cloud.LoadPoint
import com.wattracker.android.cloud.PowerCurve
import com.wattracker.android.shell.Panel
import com.wattracker.android.ui.theme.Palette
import java.util.Locale
import kotlin.math.ln

private val ColorCtl = Color(0xFF1BAF7A)
private val ColorAtl = Color(0xFFC98500)
private val ColorTsb = Color(0xFF3987E5)
private val ColorTssBar get() = Palette.muted.copy(alpha = 0.25f)

private val ColorMeasured = Color(0xFFC98500)
private val ColorAllTime = Color(0xFF3987E5)
private val ColorLastRide = Color(0xFFE05A5A)
private val ColorModel = Color(0xFF1BAF7A)

/**
 * Load chart rendering CTL, ATL and TSB lines over daily TSS bars, for a
 * selectable window.
 */
@Composable
fun LoadChart(
    points: List<LoadPoint>,
    selectedWindow: LoadWindow,
    onWindowSelected: (LoadWindow) -> Unit,
    modifier: Modifier = Modifier,
) {
    Panel(modifier = modifier) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = stringResource(R.string.dashboard_chart_load_title),
                    style = TextStyle(
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = Palette.textBright,
                    ),
                )
                WindowSelector(
                    selectedWindow = selectedWindow,
                    onWindowSelected = onWindowSelected,
                )
            }

            if (points.isEmpty()) {
                EmptyChartMessage(message = stringResource(R.string.dashboard_no_load_history))
            } else {
                LoadCanvasChart(points = points)
                LoadLegend()
            }
        }
    }
}

@Composable
private fun WindowSelector(
    selectedWindow: LoadWindow,
    onWindowSelected: (LoadWindow) -> Unit,
) {
    Row(
        modifier = Modifier
            .background(Palette.surfaceInset, RoundedCornerShape(6.dp))
            .border(1.dp, Palette.surfaceBorder, RoundedCornerShape(6.dp))
            .padding(2.dp),
    ) {
        LoadWindow.entries.forEach { window ->
            val isSelected = window == selectedWindow
            Box(
                modifier = Modifier
                    .background(
                        if (isSelected) Palette.surface2 else Color.Transparent,
                        RoundedCornerShape(4.dp),
                    )
                    .clickable { onWindowSelected(window) }
                    .padding(horizontal = 8.dp, vertical = 4.dp),
            ) {
                Text(
                    text = stringResource(window.labelRes),
                    style = TextStyle(
                        fontSize = 11.sp,
                        fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                        color = if (isSelected) Palette.textBright else Palette.muted,
                    ),
                )
            }
        }
    }
}

@Composable
private fun LoadCanvasChart(points: List<LoadPoint>) {
    val textMeasurer = rememberTextMeasurer()
    val paddingLeftDp = 36.dp
    val paddingRightDp = 12.dp
    val paddingTopDp = 12.dp
    val paddingBottomDp = 20.dp

    Canvas(
        modifier = Modifier
            .fillMaxWidth()
            .height(180.dp)
            // Canvas does not clip by default; a stray value must not paint
            // over the title and window selector above the chart.
            .clipToBounds(),
    ) {
        val width = size.width
        val height = size.height

        val paddingLeft = paddingLeftDp.toPx()
        val paddingRight = paddingRightDp.toPx()
        val paddingTop = paddingTopDp.toPx()
        val paddingBottom = paddingBottomDp.toPx()

        val graphWidth = width - paddingLeft - paddingRight
        val graphHeight = height - paddingTop - paddingBottom

        if (graphWidth <= 0 || graphHeight <= 0 || points.isEmpty()) return@Canvas

        val axis = loadAxisRange(points) ?: return@Canvas
        val minY = axis.start
        val maxY = axis.endInclusive
        val yRange = maxY - minY

        fun xPos(index: Int): Float {
            if (points.size <= 1) return paddingLeft + graphWidth / 2f
            return paddingLeft + (index.toFloat() / (points.size - 1)) * graphWidth
        }

        fun yPos(value: Double): Float {
            val norm = (value - minY) / yRange
            return paddingTop + graphHeight * (1f - norm.toFloat())
        }

        // Horizontal grid lines
        val gridCount = 4
        for (i in 0..gridCount) {
            val yVal = minY + (yRange * i / gridCount)
            val y = yPos(yVal)
            drawLine(
                color = Palette.surfaceBorder,
                start = Offset(paddingLeft, y),
                end = Offset(width - paddingRight, y),
                strokeWidth = 1f,
            )
            val textLayoutResult = textMeasurer.measure(
                text = String.format(Locale.US, "%.0f", yVal),
                style = TextStyle(fontSize = 10.sp, color = Palette.muted),
            )
            drawText(
                textLayoutResult = textLayoutResult,
                topLeft = Offset(4.dp.toPx(), y - textLayoutResult.size.height / 2f),
            )
        }

        // Draw Zero line if TSB dips below 0
        val zeroY = yPos(0.0)
        if (minY < 0 && maxY > 0) {
            drawLine(
                color = Palette.muted.copy(alpha = 0.4f),
                start = Offset(paddingLeft, zeroY),
                end = Offset(width - paddingRight, zeroY),
                strokeWidth = 1f,
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(6f, 4f)),
            )
        }

        // Daily TSS bars, on the same axis as the lines (one chart, one y
        // axis, as the desktop's charts do); loadAxisRange includes them.
        val barWidth = (graphWidth / points.size.coerceAtLeast(1)).coerceIn(1.dp.toPx(), 8.dp.toPx())
        points.forEachIndexed { i, pt ->
            pt.tss?.let { v ->
                if (v > 0 && v.isFinite()) {
                    val x = xPos(i)
                    val y = yPos(v)
                    drawLine(
                        color = ColorTssBar,
                        start = Offset(x, zeroY),
                        end = Offset(x, y),
                        strokeWidth = barWidth,
                    )
                }
            }
        }

        // Draw CTL line
        val ctlPath = Path()
        var ctlStarted = false
        points.forEachIndexed { i, pt ->
            pt.ctl?.let { v ->
                val x = xPos(i)
                val y = yPos(v)
                if (!ctlStarted) {
                    ctlPath.moveTo(x, y)
                    ctlStarted = true
                } else {
                    ctlPath.lineTo(x, y)
                }
            }
        }
        if (ctlStarted) {
            drawPath(path = ctlPath, color = ColorCtl, style = Stroke(width = 2.dp.toPx()))
        }

        // Draw ATL line
        val atlPath = Path()
        var atlStarted = false
        points.forEachIndexed { i, pt ->
            pt.atl?.let { v ->
                val x = xPos(i)
                val y = yPos(v)
                if (!atlStarted) {
                    atlPath.moveTo(x, y)
                    atlStarted = true
                } else {
                    atlPath.lineTo(x, y)
                }
            }
        }
        if (atlStarted) {
            drawPath(path = atlPath, color = ColorAtl, style = Stroke(width = 2.dp.toPx()))
        }

        // Draw TSB line
        val tsbPath = Path()
        var tsbStarted = false
        points.forEachIndexed { i, pt ->
            pt.tsb?.let { v ->
                val x = xPos(i)
                val y = yPos(v)
                if (!tsbStarted) {
                    tsbPath.moveTo(x, y)
                    tsbStarted = true
                } else {
                    tsbPath.lineTo(x, y)
                }
            }
        }
        if (tsbStarted) {
            drawPath(path = tsbPath, color = ColorTsb, style = Stroke(width = 1.5f.dp.toPx()))
        }
    }
}

@Composable
private fun LoadLegend() {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(16.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        LegendItem(color = ColorCtl, label = stringResource(R.string.legend_ctl))
        LegendItem(color = ColorAtl, label = stringResource(R.string.legend_atl))
        LegendItem(color = ColorTsb, label = stringResource(R.string.legend_tsb))
        LegendItem(color = ColorTssBar, label = stringResource(R.string.legend_tss))
    }
}

/**
 * Power-duration curve chart.
 */
@Composable
fun CurveChart(
    curve: PowerCurve?,
    ftp: Double?,
    modifier: Modifier = Modifier,
) {
    Panel(modifier = modifier) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(
                text = stringResource(R.string.dashboard_chart_curve_title),
                style = TextStyle(
                    fontSize = 16.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = Palette.textBright,
                ),
            )

            val hasData = curve != null && (
                !curve.measured.isNullOrEmpty() ||
                    !curve.allTime.isNullOrEmpty() ||
                    !curve.lastRide.isNullOrEmpty() ||
                    !curve.model.isNullOrEmpty()
                )

            if (!hasData) {
                EmptyChartMessage(message = stringResource(R.string.dashboard_no_curve_data))
            } else {
                CurveCanvasChart(curve = curve!!, ftp = ftp)
                CurveLegend()
            }
        }
    }
}

@Composable
private fun CurveCanvasChart(
    curve: PowerCurve,
    ftp: Double?,
) {
    val textMeasurer = rememberTextMeasurer()
    val paddingLeftDp = 36.dp
    val paddingRightDp = 12.dp
    val paddingTopDp = 12.dp
    val paddingBottomDp = 20.dp

    Canvas(
        modifier = Modifier
            .fillMaxWidth()
            .height(180.dp),
    ) {
        val width = size.width
        val height = size.height

        val paddingLeft = paddingLeftDp.toPx()
        val paddingRight = paddingRightDp.toPx()
        val paddingTop = paddingTopDp.toPx()
        val paddingBottom = paddingBottomDp.toPx()

        val graphWidth = width - paddingLeft - paddingRight
        val graphHeight = height - paddingTop - paddingBottom

        if (graphWidth <= 0 || graphHeight <= 0) return@Canvas

        val allSeries = listOf(
            curve.measured ?: emptyList(),
            curve.allTime ?: emptyList(),
            curve.lastRide ?: emptyList(),
            curve.model ?: emptyList(),
        )

        val allPowerValues = mutableListOf<Double>()
        var minDur = Double.MAX_VALUE
        var maxDur = 0.0

        allSeries.forEach { list ->
            list.forEach { pt ->
                val p = pt.power
                val t = pt.t
                if (p != null && p.isFinite() && p > 0) allPowerValues.add(p)
                if (t != null && t.isFinite() && t > 0) {
                    if (t < minDur) minDur = t
                    if (t > maxDur) maxDur = t
                }
            }
        }

        if (allPowerValues.isEmpty() || maxDur <= 0) return@Canvas

        if (minDur <= 0 || minDur >= maxDur) minDur = 1.0

        val maxPower = (allPowerValues.maxOrNull() ?: 300.0).coerceAtLeast(ftp ?: 200.0)
        val minPower = 0.0

        val logMinDur = ln(minDur)
        val logMaxDur = ln(maxDur)
        val logRangeDur = if (logMaxDur > logMinDur) logMaxDur - logMinDur else 1.0

        fun xPos(durationSec: Double): Float {
            val logVal = ln(durationSec.coerceIn(minDur, maxDur))
            val norm = (logVal - logMinDur) / logRangeDur
            return paddingLeft + norm.toFloat() * graphWidth
        }

        fun yPos(powerW: Double): Float {
            val norm = (powerW - minPower) / (maxPower - minPower)
            return paddingTop + graphHeight * (1f - norm.toFloat())
        }

        // Horizontal Grid
        val gridCount = 4
        for (i in 0..gridCount) {
            val pVal = minPower + ((maxPower - minPower) * i / gridCount)
            val y = yPos(pVal)
            drawLine(
                color = Palette.surfaceBorder,
                start = Offset(paddingLeft, y),
                end = Offset(width - paddingRight, y),
                strokeWidth = 1f,
            )
            val textLayoutResult = textMeasurer.measure(
                text = "${pVal.toInt()}W",
                style = TextStyle(fontSize = 10.sp, color = Palette.muted),
            )
            drawText(
                textLayoutResult = textLayoutResult,
                topLeft = Offset(4.dp.toPx(), y - textLayoutResult.size.height / 2f),
            )
        }

        // Draw FTP reference line if available
        if (ftp != null && ftp > 0 && ftp <= maxPower) {
            val ftpY = yPos(ftp)
            drawLine(
                color = Palette.muted,
                start = Offset(paddingLeft, ftpY),
                end = Offset(width - paddingRight, ftpY),
                strokeWidth = 1.5f,
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(6f, 4f)),
            )
            val ftpTextResult = textMeasurer.measure(
                text = "FTP ${ftp.toInt()}W",
                style = TextStyle(fontSize = 10.sp, color = Palette.muted, fontWeight = FontWeight.Bold),
            )
            drawText(
                textLayoutResult = ftpTextResult,
                topLeft = Offset(width - paddingRight - ftpTextResult.size.width, ftpY - ftpTextResult.size.height - 2f),
            )
        }

        // Helper to draw a curve series
        fun drawCurve(series: List<CurvePoint>, color: Color, strokeWidthPx: Float) {
            val valid = series.filter { (it.t ?: 0.0) > 0 && (it.power ?: 0.0) > 0 }
            if (valid.isEmpty()) return
            val path = Path()
            valid.forEachIndexed { i, pt ->
                val x = xPos(pt.t!!)
                val y = yPos(pt.power!!)
                if (i == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            drawPath(path = path, color = color, style = Stroke(width = strokeWidthPx))
        }

        drawCurve(curve.allTime ?: emptyList(), ColorAllTime, 1.5f.dp.toPx())
        drawCurve(curve.measured ?: emptyList(), ColorMeasured, 2f.dp.toPx())
        drawCurve(curve.lastRide ?: emptyList(), ColorLastRide, 1.5f.dp.toPx())
        drawCurve(curve.model ?: emptyList(), ColorModel, 1.5f.dp.toPx())
    }
}

@Composable
private fun CurveLegend() {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        LegendItem(color = ColorMeasured, label = stringResource(R.string.legend_last_90d))
        LegendItem(color = ColorAllTime, label = stringResource(R.string.legend_all_time))
        LegendItem(color = ColorLastRide, label = stringResource(R.string.legend_last_ride))
        LegendItem(color = ColorModel, label = stringResource(R.string.legend_model))
    }
}

@Composable
private fun LegendItem(color: Color, label: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            modifier = Modifier
                .size(10.dp)
                .background(color, RoundedCornerShape(2.dp)),
        )
        Spacer(modifier = Modifier.width(4.dp))
        Text(
            text = label,
            style = TextStyle(fontSize = 11.sp, color = Palette.muted),
        )
    }
}

@Composable
private fun EmptyChartMessage(message: String) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(140.dp)
            .background(Palette.surfaceInset, RoundedCornerShape(6.dp)),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = message,
            style = TextStyle(fontSize = 13.sp, color = Palette.muted),
        )
    }
}
