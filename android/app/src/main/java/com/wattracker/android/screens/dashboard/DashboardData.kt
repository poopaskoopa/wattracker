package com.wattracker.android.screens.dashboard

import com.wattracker.android.R
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.LoadPoint
import com.wattracker.android.cloud.PowerCurve
import com.wattracker.android.cloud.RiderProfile
import com.wattracker.android.cloud.TrainingState
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Window selection for the training load history chart.
 */
enum class LoadWindow(val labelRes: Int, val days: Int) {
    SIX_WEEKS(R.string.window_6_weeks, 42),
    SIX_MONTHS(R.string.window_6_months, 183),
    ONE_YEAR(R.string.window_1_year, 365),
}

/**
 * The load chart's single y axis: every plotted value, TSS bars included.
 *
 * The bars share the lines' axis rather than getting a scale of their own, so
 * a 250-TSS day sets the top of the chart instead of being drawn past it.
 * Zero is always in range because the bars grow from it. Null when nothing in
 * [points] can be plotted.
 */
fun loadAxisRange(points: List<LoadPoint>): ClosedFloatingPointRange<Double>? {
    val values = points.flatMap { listOfNotNull(it.ctl, it.atl, it.tsb, it.tss) }.filter { it.isFinite() }
    if (values.isEmpty()) return null
    val min = minOf(values.min(), 0.0)
    var max = maxOf(values.max(), 0.0)
    if (max <= min) max = min + 10.0
    return min..max
}

/**
 * The subset of a dashboard snapshot the screen needs to render.
 */
data class DashboardData(
    val profile: RiderProfile?,
    val training: TrainingState?,
    val curve: PowerCurve?,
    val loadPoints: List<LoadPoint>,
    val activities: List<ActivitySummary>,
    val source: CloudSnapshot.Source,
    val asOf: Long,
) {
    companion object {
        /**
         * The recent-rides order. Stores hand activities back sorted by object
         * id as text, which is not date order, so every list is sorted here.
         */
        val newestFirst: Comparator<ActivitySummary> = Comparator { lhs, rhs ->
            (rhs.startTime ?: "").compareTo(lhs.startTime ?: "")
        }

        /** The live `activity` summaries in [snapshot], newest first. */
        fun activitiesFrom(snapshot: CloudSnapshot): List<ActivitySummary> =
            snapshot.items
                .filter { !it.deleted }
                .mapNotNull { (it.payload as? CloudPayload.Activity)?.value }
                .sortedWith(newestFirst)

        fun fromSnapshot(snapshot: CloudSnapshot): DashboardData {
            var profile: RiderProfile? = null
            var training: TrainingState? = null
            var curve: PowerCurve? = null
            val points = mutableListOf<LoadPoint>()
            val activities = mutableListOf<ActivitySummary>()

            for (item in snapshot.items) {
                if (item.deleted) continue
                when (val payload = item.payload) {
                    is CloudPayload.Profile -> profile = payload.value
                    is CloudPayload.TrainingState -> training = payload.value
                    is CloudPayload.LoadPoint -> points.add(payload.value)
                    is CloudPayload.Curve -> curve = payload.value
                    is CloudPayload.Activity -> activities.add(payload.value)
                    else -> {}
                }
            }

            val sortedPoints = points.sortedWith { lhs, rhs ->
                val leftDate = parseDate(lhs.date)
                val rightDate = parseDate(rhs.date)
                when {
                    leftDate != null && rightDate != null -> leftDate.compareTo(rightDate)
                    leftDate != null -> -1
                    rightDate != null -> 1
                    else -> (lhs.date ?: "").compareTo(rhs.date ?: "")
                }
            }

            val sortedActivities = activities.sortedWith(newestFirst)

            return DashboardData(
                profile = profile,
                training = training,
                curve = curve,
                loadPoints = sortedPoints,
                activities = sortedActivities,
                source = snapshot.source,
                asOf = snapshot.asOf,
            )
        }

        fun parseDate(value: String?): Date? {
            if (value.isNullOrBlank()) return null
            val formats = listOf(
                "yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX",
                "yyyy-MM-dd'T'HH:mm:ssXXXXX",
                "yyyy-MM-dd'T'HH:mm:ss.SSS",
                "yyyy-MM-dd'T'HH:mm:ss",
                "yyyy-MM-dd",
            )
            for (format in formats) {
                try {
                    val sdf = SimpleDateFormat(format, Locale.US)
                    sdf.timeZone = TimeZone.getTimeZone("UTC")
                    val date = sdf.parse(value)
                    if (date != null) return date
                } catch (_: Exception) {
                    // Try next format
                }
            }
            return null
        }

        fun formatNumber(value: Double?, suffix: String = ""): String {
            if (value == null || !value.isFinite()) return "—"
            val formatted = String.format(Locale.US, "%.1f", value)
            val trimmed = if (formatted.endsWith(".0")) {
                formatted.dropLast(2)
            } else {
                formatted
            }
            return trimmed + suffix
        }
    }

    /** The rider's current FTP if available from training state or profile. */
    val currentFTP: Double?
        get() = training?.ftp ?: profile?.resolvedFTP

    /** The rider's Critical Power if available from training state or curve. */
    val currentCP: Double?
        get() = training?.cp ?: curve?.cp

    /** The rider's W' if available from training state or curve. */
    val currentWPrime: Double?
        get() = training?.wprime ?: curve?.wprime

    /** Weight in kg if published. */
    val weightKg: Double?
        get() = profile?.weightKg

    /** The newest usable load record. */
    val latestLoadPoint: LoadPoint?
        get() = loadPoints
            .filter { parseDate(it.date) != null && (it.ctl != null || it.atl != null || it.tsb != null) }
            .maxByOrNull { parseDate(it.date)?.time ?: 0L }

    val currentCTL: Double?
        get() = training?.ctl ?: latestLoadPoint?.ctl

    val currentATL: Double?
        get() = training?.atl ?: latestLoadPoint?.atl

    val currentTSB: Double?
        get() = training?.tsb ?: latestLoadPoint?.tsb

    /**
     * Whether the snapshot has any usable data.
     */
    val hasAnyData: Boolean
        get() {
            if (loadPoints.any { it.tss != null || it.ctl != null || it.atl != null || it.tsb != null }) {
                return true
            }
            if (profile?.resolvedFTP != null || profile?.weightKg != null || profile?.power?.available == true || profile?.heartRate?.available == true) {
                return true
            }
            if (training?.ftp != null || training?.cp != null || training?.wprime != null || training?.decoupling != null) {
                return true
            }
            val c = curve ?: return false
            return !c.measured.isNullOrEmpty() || !c.allTime.isNullOrEmpty() || !c.lastRide.isNullOrEmpty() || !c.model.isNullOrEmpty() || c.cp != null || c.wprime != null
        }

    /**
     * Filters load points for the selected window relative to the newest point's date in snapshot.
     */
    fun points(window: LoadWindow): List<LoadPoint> {
        val latestDate = loadPoints.mapNotNull { parseDate(it.date) }.maxOrNull() ?: return emptyList()
        val cal = Calendar.getInstance(TimeZone.getTimeZone("UTC")).apply {
            time = latestDate
            add(Calendar.DAY_OF_YEAR, -window.days)
        }
        val cutoff = cal.time
        return loadPoints.filter { point ->
            val date = parseDate(point.date) ?: return@filter false
            date >= cutoff && date <= latestDate
        }
    }
}
