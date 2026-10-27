package com.wattracker.android.screens.volume

import com.wattracker.android.R
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.VolumeWeek
import com.wattracker.android.cloud.isoDateOrNull
import java.time.DayOfWeek
import java.time.LocalDate
import java.time.temporal.TemporalAdjusters
import java.util.Locale

/**
 * The volume route read for the screen -- a line-for-line port of the iOS
 * `VolumeMetric` / `VolumeRange` / `VolumeSummary` / `VolumeData` block from
 * `MobileScreenData.swift`.
 *
 * Weeks arrive as the desktop publishes them: Monday-anchored, rider-local,
 * already bucketed. Nothing here re-buckets: dates are the published ones,
 * and a client-side re-bucket is a day/week-boundary bug waiting to happen
 * (it already happened once on the desktop side).
 */

/** The four published weekly totals. */
enum class VolumeMetric(val titleRes: Int, val unit: String) {
    Hours(R.string.volume_metric_hours, "h"),
    Tss(R.string.volume_metric_tss, "TSS"),
    DistanceKm(R.string.volume_metric_distance, "km"),
    Calories(R.string.volume_metric_calories, "kcal"),
    ;

    fun valueFrom(week: VolumeWeek): Double = when (this) {
        VolumeMetric.Hours -> week.hours ?: 0.0
        VolumeMetric.Tss -> week.tss ?: 0.0
        VolumeMetric.DistanceKm -> week.distanceKm ?: 0.0
        VolumeMetric.Calories -> week.calories ?: 0.0
    }
}

/** The windows the chart and the tiles are cut on. */
enum class VolumeRange(val titleRes: Int, val shortTitleRes: Int, val weeks: Int?) {
    Last4(R.string.volume_range_last_4, R.string.volume_range_last_4_short, 4),
    Last12(R.string.volume_range_last_12, R.string.volume_range_last_12_short, 12),
    YearToDate(R.string.volume_range_ytd, R.string.volume_range_ytd_short, null),
    ;
}

/** The totals of one window of weeks. */
data class VolumeSummary(
    val hours: Double,
    val tss: Double,
    val distanceKm: Double,
    val calories: Double,
) {
    constructor(weeks: List<VolumeWeek>) : this(
        hours = weeks.sumOf { it.hours ?: 0.0 },
        tss = weeks.sumOf { it.tss ?: 0.0 },
        distanceKm = weeks.sumOf { it.distanceKm ?: 0.0 },
        calories = weeks.sumOf { it.calories ?: 0.0 },
    )

    fun valueFor(metric: VolumeMetric): Double = when (metric) {
        VolumeMetric.Hours -> hours
        VolumeMetric.Tss -> tss
        VolumeMetric.DistanceKm -> distanceKm
        VolumeMetric.Calories -> calories
    }
}

/**
 * Volume objects prepared for the chart and the summary tiles.
 */
data class VolumeData(
    val weeks: List<VolumeWeek>,
    /**
     * The weeks with the gaps filled: the server omits weeks with no rides,
     * and a zero bucket keeps the chart's spacing and the rolling summaries
     * honest about rest weeks.
     */
    val denseWeeks: List<VolumeWeek>,
    val source: CloudSnapshot.Source,
    val asOf: Long,
) {
    /**
     * One snapshot, read once: the dense fill works on the same list the
     * screen shows, so parsing the snapshot twice only doubled the walk (and
     * left two chances for the two halves to disagree).
     */
    constructor(snapshot: CloudSnapshot) : this(parse(snapshot), snapshot.source, snapshot.asOf)

    private constructor(
        weeks: List<VolumeWeek>,
        source: CloudSnapshot.Source,
        asOf: Long,
    ) : this(
        weeks = weeks,
        denseWeeks = densify(weeks),
        source = source,
        asOf = asOf,
    )

    companion object {
        private fun parse(snapshot: CloudSnapshot): List<VolumeWeek> {
            // One object per week is the reconciled normal; if an older cache
            // holds duplicates, keep the last rather than drawing the week
            // twice (the iOS reader's rule).
            val byStart = LinkedHashMap<String, VolumeWeek>()
            for (item in snapshot.items) {
                if (item.deleted) continue
                val week = (item.payload as? CloudPayload.VolumeWeek)?.value ?: continue
                // A week start this client cannot read belongs to no bucket.
                val start = week.weekStart?.let { isoDateOrNull(it) } ?: continue
                byStart[start.toString()] = week
            }
            return byStart.values.sortedBy { it.weekStart ?: "" }
        }

        /**
         * Fill the gaps between published buckets with zero weeks.
         *
         * The zero weeks ride the server's cadence, and the cadence is the
         * server's own Monday anchor: the grid steps seven days at a time from
         * the Monday of the first published week (#456 review, finding 3 --
         * stepping from the first bucket itself means that a bucket published on
         * any other weekday silently vanished from every window and every
         * total). Anchoring on the Monday changes nothing for the normal case,
         * where the server does publish Mondays, and keeps the spacing the
         * desktop's even when it does not.
         *
         * A published bucket that falls off the cadence is still emitted, at
         * its own date with its own totals, and it does not shift the grid:
         * a week the server published is never invisible to the rider, and
         * re-bucketing it here is exactly the day-boundary bug this file
         * refuses to commit.
         *
         * Only the zero fill is bounded: a bogus ancient bucket must not be
         * able to ask for a million-row chart. Published weeks never are.
         */
        private fun densify(weeks: List<VolumeWeek>): List<VolumeWeek> {
            val first = weeks.firstOrNull()?.weekStart?.let { isoDateOrNull(it) } ?: return emptyList()
            val output = ArrayList<VolumeWeek>(weeks.size)
            var slot = first.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY))
            var zeros = 0
            for (week in weeks) {
                // `parse` returns the buckets sorted and de-duplicated (an
                // older cache may hold two objects for one week: keep the
                // last), so this walk is monotonic in both the fill and the
                // buckets themselves.
                val start = week.weekStart?.let { isoDateOrNull(it) } ?: continue
                while (slot.isBefore(start) && zeros < MAX_ZERO_WEEKS) {
                    output.add(
                        VolumeWeek(slot.toString(), hours = 0.0, tss = 0.0, distanceKm = 0.0, calories = 0.0),
                    )
                    zeros += 1
                    slot = slot.plusDays(7)
                }
                output.add(week)
                if (slot == start) slot = slot.plusDays(7)
            }
            return output
        }

        /** The bound on the zero fill, not on the weeks the server published. */
        private const val MAX_ZERO_WEEKS = 5000
    }

    /** The window's weeks: the last N dense weeks, or this year's buckets. */
    fun weeksFor(range: VolumeRange, referenceDate: LocalDate = LocalDate.now()): List<VolumeWeek> {
        val dense = denseWeeks
        if (dense.isEmpty()) return emptyList()
        return when (range) {
            VolumeRange.Last4, VolumeRange.Last12 -> dense.takeLast(range.weeks ?: 0)
            VolumeRange.YearToDate -> {
                // Volume is published as Monday buckets. A bucket belongs to
                // the year of its Monday, so the first partial week of a new
                // year is intentionally kept with the prior year's bucket.
                val year = referenceDate.year
                dense.filter { week ->
                    isoDateOrNull(week.weekStart)?.year == year
                }
            }
        }
    }

    /**
     * The window of the same size immediately before [range], when both
     * windows would be complete; empty otherwise, because an equal-length
     * comparison across an incomplete span is noise.
     */
    fun previousWeeks(range: VolumeRange): List<VolumeWeek> {
        val count = range.weeks ?: return emptyList()
        val dense = denseWeeks
        if (dense.size < count * 2) return emptyList()
        val end = dense.size - count
        return dense.subList(end - count, end)
    }

    fun summaryFor(range: VolumeRange, referenceDate: LocalDate = LocalDate.now()): VolumeSummary =
        VolumeSummary(weeksFor(range, referenceDate))

    /**
     * The selected metric's change against the previous equal window, or
     * null when there is no previous window to compare against -- a change
     * out of zero is not a percentage.
     */
    fun change(metric: VolumeMetric, range: VolumeRange, referenceDate: LocalDate = LocalDate.now()): Double? {
        val previous = VolumeSummary(previousWeeks(range)).valueFor(metric)
        if (previous <= 0) return null
        val current = summaryFor(range, referenceDate).valueFor(metric)
        return (current - previous) / previous
    }
}

/** The volume numbers, formatted the iOS way: one decimal for the measured totals, none for the counted ones. */
object VolumeFormat {

    fun value(value: Double, metric: VolumeMetric): String {
        val text = when (metric) {
            VolumeMetric.Hours, VolumeMetric.DistanceKm -> String.format(Locale.US, "%.1f", value)
            else -> String.format(Locale.US, "%.0f", value)
        }
        return "$text ${metric.unit}"
    }

    /**
     * A change as a signed whole percent. No sign is printed for zero: the
     * desktop prints `pct.toFixed(0)` with the "+" only when the number is
     * above zero, and a change of exactly zero gets its own flat marker in the
     * screen rather than a "+0%" wearing an up arrow.
     */
    fun percent(value: Double): String =
        String.format(Locale.US, "%s%.0f%%", if (value > 0.0) "+" else "", value * 100)
}
