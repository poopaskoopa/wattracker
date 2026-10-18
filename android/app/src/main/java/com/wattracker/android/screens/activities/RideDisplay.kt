package com.wattracker.android.screens.activities

import androidx.compose.ui.graphics.Color
import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.opt
import com.wattracker.android.json.optDouble
import com.wattracker.android.json.optString
import com.wattracker.android.ui.theme.Palette
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.round
import kotlin.math.roundToLong

// MARK: - The list

/**
 * One ride in the list: the published `activity` object plus what a row
 * renders. Mirrors the iOS `RideSummary`.
 */
data class RideSummary(
    val id: String,
    val activityId: Int,
    val summary: ActivitySummary,
    val startedAt: String,
) {
    companion object {
        /**
         * The list order: newest first by published start time, the object id
         * as the tie-break. Mirrors the iOS `rides` computed property.
         */
        val newestFirst: Comparator<RideSummary> = Comparator { lhs, rhs ->
            if (lhs.startedAt != rhs.startedAt) rhs.startedAt.compareTo(lhs.startedAt)
            else rhs.id.compareTo(lhs.id)
        }

        /**
         * A live ride, or null for a tombstone or a non-`activity` payload.
         * The numeric id falls back to the object id's suffix, the way iOS
         * reads it.
         */
        fun from(item: CloudItem): RideSummary? {
            if (item.deleted) return null
            val summary = (item.payload as? CloudPayload.Activity)?.value ?: return null
            val numericId = summary.id?.toInt()
                ?: item.id.substringAfterLast('-').toIntOrNull()
            if (numericId == null) return null
            return RideSummary(item.id, numericId, summary, summary.startTime ?: "")
        }
    }
}

// MARK: - Formatting

/**
 * The ride numbers, formatted the way the desktop formats them.
 *
 * Missing values render as the same em-dash the dashboard uses: a gap in the
 * data, not an error. Duration rounding is half-to-even, the way Python's
 * `round()` and Swift's `.toNearestOrEven` both do it, so a boundary second
 * agrees across the three implementations.
 */
object RideFormatting {
    /** The ride's start, in the device's local zone, or null when absent. */
    fun date(value: String?): String? {
        if (value == null) return null
        val formats = listOf(
            "yyyy-MM-dd'T'HH:mm:ss.SSSXXX",
            "yyyy-MM-dd'T'HH:mm:ssXXX",
            "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "yyyy-MM-dd'T'HH:mm:ss",
        )
        for (pattern in formats) {
            val parsed = try {
                val sdf = SimpleDateFormat(pattern, Locale.US)
                sdf.timeZone = TimeZone.getTimeZone("UTC")
                // A full match only: the default parse reads a prefix and
                // would accept "2026-01-02T18:04:00<garbage>".
                val pos = java.text.ParsePosition(0)
                val date = sdf.parse(value, pos)
                if (pos.index == value.length) date else null
            } catch (_: Exception) {
                null
            }
            if (parsed != null) {
                return SimpleDateFormat("MMM d, yyyy, h:mm a", Locale.getDefault()).format(parsed)
            }
        }
        // Not a timestamp this client will parse: show what was published,
        // with the T for a space, rather than guessing.
        return value.replace("T", " ")
    }

    /** `mm:ss`, zero-padded; `h:mm:ss` past an hour. Half-to-even at half seconds. */
    fun duration(seconds: Double?): String {
        if (seconds == null || !seconds.isFinite() || seconds < 0) return "—"
        val total = Math.rint(seconds).toLong()
        val hours = total / 3_600
        val minutes = (total % 3_600) / 60
        val remainder = total % 60
        return if (hours > 0) {
            String.format(Locale.US, "%d:%02d:%02d", hours, minutes, remainder)
        } else {
            String.format(Locale.US, "%02d:%02d", minutes, remainder)
        }
    }

    fun distance(meters: Double?): String {
        if (meters == null || !meters.isFinite()) return "—"
        return String.format(Locale.US, "%.1f km", meters / 1_000.0)
    }

    fun watts(value: Double?): String {
        if (value == null || !value.isFinite()) return "—"
        return "${Math.round(value).toInt()} W"
    }

    fun decimal(value: Double?, places: Int): String {
        if (value == null || !value.isFinite()) return "—"
        return String.format(Locale.US, "%.${places}f", value)
    }
}

/**
 * Zone times from `activity_detail.zones`. The zone rows carry their own
 * `duration` string from the server, which is preferred; the local fallback
 * uses the zone formatter (minutes not padded), not the ride card's.
 * Mirrors the iOS `ZoneGroup`/`ZoneRow`/`ZoneFormatting`.
 */
object ZoneFormatting {
    fun duration(seconds: Double?): String {
        if (seconds == null || !seconds.isFinite() || seconds < 0) return "—"
        val total = Math.rint(seconds).toLong()
        val hours = total / 3_600
        val minutes = (total % 3_600) / 60
        val remainder = total % 60
        return if (hours > 0) {
            String.format(Locale.US, "%d:%02d:%02d", hours, minutes, remainder)
        } else {
            String.format(Locale.US, "%d:%02d", minutes, remainder)
        }
    }
}

data class ZoneRow(
    val id: String,
    val label: String,
    val seconds: Double,
    val percent: Double,
    val duration: String?,
) {
    /** The server's string when published, the local formatter otherwise. */
    val durationText: String
        get() = duration ?: ZoneFormatting.duration(seconds)
}

data class ZoneGroup(
    val id: String,
    val title: String,
    val rows: List<ZoneRow>,
) {
    companion object {
        /**
         * The two zone blocks of an `activity_detail.zones` payload. A block
         * is absent (or carries no `zones` array) when the ride had no stream
         * or no anchor for it; a row with no time in it is not rendered.
         */
        fun extract(zones: JsonValue?): List<ZoneGroup> {
            if (zones == null) return emptyList()
            return listOf("power" to "Power zones", "heart_rate" to "Heart-rate zones")
                .mapNotNull { (key, title) ->
                    val block = zones.opt(key)
                        as? JsonValue.Object
                        ?: return@mapNotNull null
                    val rowsArray = block.opt("zones")
                        as? JsonValue.Array
                        ?: return@mapNotNull null
                    val rows = rowsArray.values.mapIndexedNotNull { index, row ->
                        val seconds = row.optDouble("seconds")
                        if (seconds == null || seconds <= 0) return@mapIndexedNotNull null
                        ZoneRow(
                            id = "$key-$index",
                            label = row.optString("label") ?: "Z${index + 1}",
                            seconds = seconds,
                            percent = row.optDouble("percent") ?: 0.0,
                            duration = row.optString("duration"),
                        )
                    }
                    if (rows.isEmpty()) return@mapNotNull null
                    ZoneGroup(id = key, title = title, rows = rows)
                }
        }
    }
}

// MARK: - The ride graphs chart

/**
 * One sample of a stream channel, at its true position.
 *
 * [id] is the sample's index in the published channel (the position in the
 * original array, so dropped nil samples leave gaps rather than shifting the
 * rest); [time] is the seconds the published `time` channel says, or the
 * index as seconds when it has no finite value there.
 */
data class StreamPoint(val id: Int, val time: Double, val value: Double)

/**
 * One plottable channel of a ride's streams. A channel that is absent, empty
 * or all nil/non-finite has no series; nil samples are skipped individually.
 * Mirrors the iOS `StreamSeries`.
 */
data class StreamSeries(
    val id: String,
    val title: String,
    val color: Color,
    val points: List<StreamPoint>,
) {
    companion object {
        /**
         * The channels in plot order, minus the ones with nothing to plot.
         * The titles and colours are the parity spec's display strings
         * (`docs/mobile-parity.md`, "Ride graphs chart"): power on the accent
         * gold, heart rate on the HR pink, cadence on the ok green, altitude
         * on the muted grey (a background, not a data colour).
         */
        fun all(payload: ActivityStreams): List<StreamSeries> {
            val c = payload.streams
            return listOfNotNull(
                make("power", "Power (W)", Palette.accent, c.power, c.time),
                make("heart-rate", "Heart rate (bpm)", Palette.hr, c.heartrate, c.time),
                make("cadence", "Cadence (rpm)", Palette.ok, c.cadence, c.time),
                make("altitude", "Altitude (m)", Palette.muted, c.altitude, c.time),
            )
        }

        private fun make(
            id: String,
            title: String,
            color: Color,
            values: List<Double?>?,
            times: List<Double?>?,
        ): StreamSeries? {
            if (values == null) return null
            val points = values.mapIndexedNotNull { index, value ->
                if (value == null || !value.isFinite()) return@mapIndexedNotNull null
                val time = if (times != null && index < times.size) {
                    times[index]?.takeIf { it.isFinite() } ?: index.toDouble()
                } else {
                    index.toDouble()
                }
                StreamPoint(index, time, value)
            }
            if (points.isEmpty()) return null
            return StreamSeries(id, title, color, points)
        }
    }
}

/**
 * A "nice" axis: 1/2/5 x 10^n steps, about five intervals, bounds that
 * enclose the data and always span a positive range. Mirrors the iOS
 * `ChartTicks`/`ChartAxis`.
 */
data class TickScale(val ticks: List<Double>, val lower: Double, val upper: Double)

object ChartTicks {
    fun nice(lower: Double, upper: Double, target: Int): TickScale {
        val hi = max(upper, lower)
        val span = hi - lower
        val rough = (if (span > 1e-9) span else 1.0) / max(target, 1)
        val magnitude = 10.0.pow(floor(log10(rough)))
        val step = listOf(1.0, 2.0, 5.0, 10.0).map { it * magnitude }
            .firstOrNull { it >= rough } ?: 10 * magnitude
        val lo = floor(lower / step) * step
        var top = kotlin.math.ceil(hi / step) * step
        if (top <= lo) top = lo + step
        val count = round((top - lo) / step).toInt()
        return TickScale((0..count).map { lo + it * step }, lo, top)
    }

    /**
     * Minute ticks for 0...[minutes]: the first of 1, 2, 5, 10, 15, 20, 30,
     * 60, 120, 240 minutes that keeps the label count at about 6 (480 if
     * none), ticks at 0, step, 2*step ... up to the duration.
     */
    fun minutes(totalMinutes: Double): List<Double> {
        val minutes = totalMinutes
        if (!minutes.isFinite() || minutes <= 0) return listOf(0.0)
        val step = listOf(1.0, 2.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0, 120.0, 240.0)
            .firstOrNull { minutes / it <= 6 } ?: 480.0
        val ticks = mutableListOf(0.0)
        var t = step
        while (t <= minutes + 1e-9) {
            ticks.add(t)
            t += step
        }
        return ticks
    }
}

/** A y-axis of the combined ride chart: real-unit ticks over [lower]...[upper]. */
data class ChartAxis(
    val lower: Double,
    val upper: Double,
    val ticks: List<Double>,
    val title: String,
) {
    /** Tick positions on the shared 0...1 plot band. */
    val scaledTicks: List<Double>
        get() = ticks.map { (it - lower) / (upper - lower) }

    /** Real value at a 0...1 plot position (inverse of the series scaling). */
    fun valueAtScaled(position: Double): Double = lower + position * (upper - lower)

    companion object {
        /** Zero-based axis covering [values]; negatives extend the floor below zero. */
        fun zeroBased(values: List<Double>, title: String): ChartAxis {
            val hi = values.maxOrNull() ?: 0.0
            val lo = min(0.0, values.minOrNull() ?: 0.0)
            val scale = ChartTicks.nice(lower = lo, upper = hi, target = 5)
            return ChartAxis(scale.lower, scale.upper, scale.ticks, title)
        }
    }
}

/**
 * Converts a stream sample to true elapsed minutes.
 *
 * The published `time` channel is effectively all nil on the cloud (the
 * desktop stores ISO timestamps and the numeric downsampler drops them), so
 * falling back to the sample index as seconds would label a 1,500-point
 * downsample of an 86-minute ride 0...25 minutes. With the ride duration
 * known the samples are spread evenly over it; without it, 1 Hz is assumed.
 * Mirrors the iOS `MinutesClock`.
 */
class MinutesClock(
    private val channels: ActivityStreams.Channels,
    durationSeconds: Double?,
) {
    private val hasTime: Boolean = (channels.time ?: emptyList()).any { it != null && it.isFinite() }
    private val count: Int = listOf(
        channels.power, channels.heartrate, channels.cadence, channels.altitude, channels.time,
    ).mapNotNull { it?.size }.maxOrNull() ?: 0
    private val durationSeconds: Double? = durationSeconds?.takeIf { it.isFinite() && it > 0 }

    fun minutesFor(point: StreamPoint): Double = when {
        hasTime -> point.time / 60
        durationSeconds != null && count > 1 ->
            point.id.toDouble() / (count - 1) * durationSeconds / 60
        else -> point.id.toDouble() / 60
    }
}

/**
 * One series of the combined ride chart, rescaled onto a shared 0...1 band.
 *
 * Mirrors the iOS `CombinedChartSeries` (and the desktop's
 * `renderActivityDetail`): power has its own left axis, heart rate and
 * cadence share one right axis (both zero-based), and altitude is a faint
 * filled background on an auto min...max scale with no axis.
 */
data class CombinedChartSeries(
    val id: String,
    val title: String,
    val color: Color,
    val isBackground: Boolean,
    val lower: Double,
    val upper: Double,
    val points: List<Point>,
) {
    data class Point(val id: Int, val minutes: Double, val value: Double, val scaled: Double)

    /** The chart's two axes, and which channels feed each. */
    enum class Side { power, shared }

    companion object {
        /** Altitude is drawn as fill 0.18 / line 0.5 of the muted colour. */
        const val altitudeFillOpacity = 0.18f
        const val altitudeLineOpacity = 0.5f

        /**
         * Altitude first (drawn behind), then power, heart rate, cadence.
         * [durationSeconds] is the ride's `duration_s`; the calendar feeds it
         * from the activity payload the same way.
         */
        fun build(payload: ActivityStreams, durationSeconds: Double? = null): List<CombinedChartSeries> {
            val all = StreamSeries.all(payload)
            val order = listOf("altitude", "power", "heart-rate", "cadence")
            val clock = MinutesClock(payload.streams, durationSeconds)
            val powerAxis = axis(Side.power, all)
            val sharedAxis = axis(Side.shared, all)
            return order.mapNotNull { id ->
                val source = all.firstOrNull { it.id == id } ?: return@mapNotNull null
                val isAltitude = id == "altitude"
                val values = source.points.map { it.value }
                val lo: Double
                val hi: Double
                if (isAltitude) {
                    lo = values.minOrNull() ?: 0.0
                    hi = values.maxOrNull() ?: 0.0
                } else {
                    val a = if (id == "power") powerAxis else sharedAxis
                    lo = a?.lower ?: 0.0
                    hi = a?.upper ?: 1.0
                }
                val span = hi - lo
                val points = source.points.map { p ->
                    Point(
                        id = p.id,
                        minutes = clock.minutesFor(p),
                        value = p.value,
                        // A flat altitude (max == min) plots at mid-band.
                        scaled = if (span > 0) (p.value - lo) / span else 0.5,
                    )
                }
                CombinedChartSeries(
                    id = id,
                    title = source.title,
                    color = if (isAltitude) Palette.muted else source.color,
                    isBackground = isAltitude,
                    lower = lo,
                    upper = hi,
                    points = points,
                )
            }
        }

        /**
         * The left (power, "W") or right (heart rate + cadence, "bpm / rpm")
         * axis, built from every series on that side whether or not it is
         * hidden, so toggling the legend never rescales the plot.
         */
        fun axis(side: Side, all: List<StreamSeries>): ChartAxis? {
            val ids = if (side == Side.power) setOf("power") else setOf("heart-rate", "cadence")
            val values = all.filter { ids.contains(it.id) }.flatMap { s -> s.points.map { it.value } }
            if (values.isEmpty()) return null
            return ChartAxis.zeroBased(values, if (side == Side.power) "W" else "bpm / rpm")
        }

        /** The x-axis extent in minutes (0...ride length) for a built chart. */
        fun extent(series: List<CombinedChartSeries>): Double =
            series.flatMap { it.points }.maxOfOrNull { it.minutes } ?: 0.0
    }
}

/**
 * A chart tick label: plain numbers, one decimal place only when the value
 * is not whole. Mirrors the iOS `StreamCharts.tickLabel`.
 */
fun chartTickLabel(value: Double): String {
    val rounded = (value * 10).roundToLong() / 10.0
    return if (rounded == rounded.roundToLong().toDouble()) {
        rounded.toLong().toString()
    } else {
        String.format(Locale.US, "%.1f", rounded)
    }
}
