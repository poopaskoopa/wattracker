package com.wattracker.android.screens.calendar

import com.wattracker.android.cloud.CalendarMonth
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.RiderProfile
import com.wattracker.android.cloud.TrainingState
import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.opt
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optBoolean
import com.wattracker.android.json.optDouble
import com.wattracker.android.json.optString
import com.wattracker.android.screens.PublishedTime
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.roundToLong

/**
 * The calendar route after its wire objects have been read for the screen.
 * A line-for-line port of the iOS `CalendarScreen.swift` / `MobileScreenData.swift`
 * model half: days merged from parts, linked rides joined in, workout
 * profiles decoded with the desktop's own validation, and every display
 * string formatted the way iOS formats it so the two phones show the same
 * month.
 *
 * The `workouts` and `activities` inside a day stay as raw [JsonValue] on
 * purpose, as they do on iOS and in the `CalendarDay` model: the calendar's
 * schema is the desktop's, and a typed mirror here would turn one added
 * column into a decode failure for a whole day.
 */

// MARK: - The day

/** A merged day from one or more `calendar_day` objects. */
data class CalendarDayEntry(
    val dateISO: String,
    val ooto: Boolean,
    val phase: String?,
    val race: JsonValue?,
    val workouts: List<JsonValue>,
    val activities: List<JsonValue>,
    /** The FTP used to turn a published %FTP target into rider watts. */
    val currentFTP: Double?,
) {
    val workoutCount: Int get() = workouts.size
    val activityCount: Int get() = activities.size

    /**
     * Whether this cell shows anything beyond its number. Phase is
     * deliberately not in here, matching iOS: a plan window stamps a phase
     * on every day it covers, so counting phase as content would light up
     * the whole month, and the accent border would stop meaning that
     * something happened or is planned on the day. The phase still shows
     * in the cell and in the day view -- it is context, not content.
     */
    val hasContent: Boolean
        get() = ooto || race != null || workouts.isNotEmpty() || activities.isNotEmpty()
}

// MARK: - The route's data

/**
 * The calendar route read for one screen pass.
 *
 * The server splits unusually large days into parts (`part`/`parts` on the
 * payload, `calendar-day-<date>-part-<n>` ids). The day is the unit the rider
 * reads, so parts are aggregated by date rather than replacing one another --
 * a busy day silently loses workouts and rides otherwise.
 */
data class CalendarData(
    val days: List<CalendarDayEntry>,
    val currentFTP: Double?,
    val source: CloudSnapshot.Source,
    val asOf: Long,
) {
    val hasContent: Boolean get() = days.any { it.hasContent }

    fun entry(dateISO: String): CalendarDayEntry? = days.firstOrNull { it.dateISO == dateISO }

    /**
     * Build the screen's day list from a calendar snapshot, joining the
     * linked rides the calendar objects deliberately omit.
     *
     * Linked rides are left out of `calendar_day` objects to avoid publishing
     * the same activity twice, so the activities collection supplies those
     * summaries when it is available; a calendar-only snapshot still renders
     * completed-workout status and remains useful offline.
     */
    constructor(
        snapshot: CloudSnapshot,
        activitySnapshot: CloudSnapshot? = null,
        ftp: Double? = null,
    ) : this(
        days = buildDays(snapshot, activitySnapshot, ftp),
        currentFTP = ftp,
        source = snapshot.source,
        asOf = snapshot.asOf,
    )

    companion object {
        private class Accumulator {
            var ooto = false
            var phase: String? = null
            var race: JsonValue? = null
            val workouts = mutableListOf<JsonValue>()
            val activities = mutableListOf<JsonValue>()
        }

        private fun buildDays(snapshot: CloudSnapshot, activitySnapshot: CloudSnapshot?, ftp: Double?): List<CalendarDayEntry> {
            val grouped = LinkedHashMap<String, Accumulator>()
            for (item in snapshot.items) {
                if (item.deleted) continue
                val day = (item.payload as? CloudPayload.CalendarDay)?.value ?: continue
                val date = day.date ?: continue
                // An unreadable date is protocol drift; it belongs to no month.
                if (CalendarMonth.fromISODate(date) == null) continue
                val value = grouped.getOrPut(date) { Accumulator() }
                value.ooto = value.ooto || (day.ooto ?: false)
                if (value.phase == null) value.phase = day.phase
                if (value.race == null) value.race = day.race?.takeUnless { it is JsonValue.Null }
                value.workouts.addAll(day.workouts ?: emptyList())
                value.activities.addAll(day.activities ?: emptyList())
            }

            val activityByID = activitySummaries(activitySnapshot)
            return grouped.keys.sorted().mapNotNull { date ->
                val value = grouped[date] ?: return@mapNotNull null
                val activities = value.activities.toMutableList()
                val knownActivityIDs = value.activities.mapNotNull { activityID(it) }.toMutableSet()
                for (workout in value.workouts) {
                    val activityId = completedActivityID(workout) ?: continue
                    val activity = activityByID[activityId] ?: continue
                    // A summary the day already carries (the unlinked set the
                    // desktop publishes) is never added a second time.
                    if (activityId in knownActivityIDs) continue
                    knownActivityIDs.add(activityId)
                    activities.add(activity)
                }
                CalendarDayEntry(date, value.ooto, value.phase, value.race, value.workouts, activities, ftp)
            }
        }

        /** The FTP used to read %FTP targets -- from the dashboard, never from a calendar row. */
        fun currentFTP(snapshot: CloudSnapshot?): Double? {
            if (snapshot == null) return null
            var training: TrainingState? = null
            var profile: RiderProfile? = null
            for (item in snapshot.items) {
                if (item.deleted) continue
                when (val payload = item.payload) {
                    is CloudPayload.TrainingState -> training = payload.value
                    is CloudPayload.Profile -> profile = payload.value
                    else -> {}
                }
            }
            for (value in listOfNotNull(training?.ftp, profile?.resolvedFTP)) {
                if (value.isFinite() && value > 0) return value
            }
            return null
        }

        /**
         * The month's calendar snapshot, plus any adjacent-month days already
         * cached that fall in its grid's leading or trailing week.
         *
         * A backend that returns one month at a time (the desktop) leaves
         * those cells empty; one whose snapshot already spans them (the cloud
         * collection) keeps its own rows, because a date the primary snapshot
         * carries is never added a second time -- [CalendarData] appends
         * every row for a date, so a duplicate would double that day's
         * workouts.
         */
        fun gridSnapshot(
            snapshot: CloudSnapshot,
            month: CalendarMonth,
            adjacent: List<CloudSnapshot?>,
        ): CloudSnapshot {
            val outside = month.gridDates().filter { !it.isInMonth }.map { it.isoDate }.toSet()
            val present = snapshot.items.mapNotNull { calendarDate(it) }.toSet()
            val extra = adjacent.filterNotNull().flatMap { it.items }.filter { item ->
                val date = calendarDate(item) ?: return@filter false
                date in outside && date !in present
            }
            if (extra.isEmpty()) return snapshot
            return CloudSnapshot(snapshot.route, snapshot.revision, snapshot.items + extra, snapshot.source, snapshot.asOf)
        }

        private fun calendarDate(item: CloudItem): String? {
            if (item.deleted) return null
            return (item.payload as? CloudPayload.CalendarDay)?.value?.date
        }

        /** The live `activity` summaries of a snapshot, by numeric id, as the day rows read them. */
        private fun activitySummaries(snapshot: CloudSnapshot?): Map<Int, JsonValue> {
            if (snapshot == null) return emptyMap()
            val result = LinkedHashMap<Int, JsonValue>()
            for (item in snapshot.items) {
                if (item.deleted) continue
                val summary = (item.payload as? CloudPayload.Activity)?.value ?: continue
                val id = summary.id?.takeIf { it.isFinite() }?.roundToInt()
                    ?: item.id.substringAfterLast('-').toIntOrNull()
                if (id == null) continue
                result[id] = activityJSON(summary)
            }
            return result
        }

        /** The numeric id of a published activity row. */
        fun activityID(value: JsonValue): Int? {
            (value.opt("id") as? JsonValue.Number)?.let { number ->
                return if (number.value.isFinite() && abs(number.value - number.value.roundToLong()) < 1e-9) {
                    number.value.roundToInt()
                } else null
            }
            return (value.opt("id") as? JsonValue.String)?.value?.toIntOrNull()
        }

        private fun completedActivityID(workout: JsonValue): Int? {
            (workout.opt("completed_activity_id") as? JsonValue.Number)?.let { number ->
                return if (number.value.isFinite() && abs(number.value - number.value.roundToLong()) < 1e-9) {
                    number.value.roundToInt()
                } else null
            }
            return (workout.opt("completed_activity_id") as? JsonValue.String)?.value?.toIntOrNull()
        }

        /** A typed summary re-issued in the desktop's row shape, which is what the rows are keyed on. */
        private fun activityJSON(summary: ActivitySummary): JsonValue {
            val fields = LinkedHashMap<String, JsonValue>()
            summary.id?.let { fields["id"] = JsonValue.Number(it) }
            summary.startTime?.let { fields["start_time"] = JsonValue.String(it) }
            summary.durationS?.let { fields["duration_s"] = JsonValue.Number(it) }
            summary.distanceM?.let { fields["distance_m"] = JsonValue.Number(it) }
            summary.avgPower?.let { fields["avg_power"] = JsonValue.Number(it) }
            summary.avgHr?.let { fields["avg_hr"] = JsonValue.Number(it) }
            summary.np?.let { fields["np"] = JsonValue.Number(it) }
            summary.intensityFactor?.let { fields["if_"] = JsonValue.Number(it) }
            summary.tss?.let { fields["tss"] = JsonValue.Number(it) }
            summary.rpe?.let { fields["rpe"] = JsonValue.Number(it) }
            return JsonValue.Object(fields)
        }
    }
}

// MARK: - Reading the desktop row shapes

/** What the desktop's `calendar.html` says a planned workout is, read from the row's flags. */
enum class WorkoutStatus {
    Completed, Missed, Skipped, Adapted, Planned,
}

/**
 * The field readers for the desktop's calendar row shapes.
 *
 * Mirrors the iOS `CalendarJSON`: every read is optional and every numeric
 * read demands a finite value, because the desktop's calendar builder writes
 * these rows from SQLite values that can be null, and a row that renders a
 * `null` as a zero lies to the rider.
 */
object CalendarJSON {

    fun string(row: JsonValue, vararg keys: String): String? {
        for (key in keys) {
            val value = (row.opt(key) as? JsonValue.String)?.value
            if (!value.isNullOrEmpty()) return value
        }
        return null
    }

    fun number(row: JsonValue, vararg keys: String): Double? {
        for (key in keys) {
            val value = (row.opt(key) as? JsonValue.Number)?.value
            if (value != null && value.isFinite()) return value
        }
        return null
    }

    fun boolean(row: JsonValue, key: String): Boolean = (row.opt(key) as? JsonValue.Bool)?.value ?: false

    /** A published value that means "this row has one": present and not JSON null. */
    fun isPresent(value: JsonValue?): Boolean = value != null && value !is JsonValue.Null

    fun title(row: JsonValue, fallback: String): String = string(row, "name", "title", "workout_name") ?: fallback

    /** The day-cell warning count: a missed or a skipped planned workout. */
    fun missedCount(workouts: List<JsonValue>): Int =
        workouts.count { boolean(it, "missed") || boolean(it, "skipped") }

    fun workoutStatus(workout: JsonValue): WorkoutStatus = when {
        isPresent(workout.opt("completed_activity_id")) -> WorkoutStatus.Completed
        boolean(workout, "missed") -> WorkoutStatus.Missed
        boolean(workout, "skipped") -> WorkoutStatus.Skipped
        boolean(workout, "adapted") -> WorkoutStatus.Adapted
        else -> WorkoutStatus.Planned
    }

    fun raceTitle(race: JsonValue): String = title(race, fallback = "Race")

    /** A published duration as "Xh Ym" / "Ym", the way the desktop writes it. */
    fun duration(row: JsonValue): String? = number(row, "duration_s")?.let { durationString(it) }

    fun durationString(seconds: Double): String {
        if (!seconds.isFinite() || seconds < 0) return "\u2014"
        val total = seconds.roundToLong()
        val hours = total / 3600
        val minutes = (total % 3600) / 60
        return if (hours > 0) "${hours}h ${minutes}m" else "${minutes}m"
    }

    fun distanceString(metres: Double): String {
        if (!metres.isFinite() || metres < 0) return "\u2014"
        return "${numberString(metres / 1000)} km"
    }

    /** One decimal, trailing `.0` dropped -- the desktop's number voice. */
    fun numberString(value: Double): String {
        if (!value.isFinite()) return "\u2014"
        val formatted = String.format(Locale.US, "%.1f", value)
        return if (formatted.endsWith(".0")) formatted.dropLast(2) else formatted
    }

    /** A date-only published value as "Tuesday, Sep 1, 2026"; the raw string when it is not one. */
    fun displayDate(value: String): String {
        val date = isoDate(value) ?: return value
        return DateTimeFormatter.ofPattern("EEEE, MMM d, yyyy", Locale.getDefault()).format(date)
    }

    /** A published timestamp as a localized instant; a date-only value as its day; else the raw string. */
    fun displayDateTime(value: String): String {
        PublishedTime.parse(value)?.let {
            return DateTimeFormatter.ofPattern("MMM d, yyyy, h:mm a", Locale.getDefault())
                .format(it.atZone(ZoneId.systemDefault()))
        }
        return displayDate(value)
    }

    private fun isoDate(value: String): LocalDate? = com.wattracker.android.cloud.isoDateOrNull(value)
}

// MARK: - Workout profiles

/**
 * One block of a planned workout's power profile.
 *
 * `target_start`/`target_end` are fractions of FTP as published (1.0 is FTP
 * itself); [free] marks the "max effort, no target" blocks the coach writes.
 */
data class CalendarWorkoutProfileBlock(
    val start: Double,
    val end: Double,
    val durationS: Double,
    val targetStart: Double?,
    val targetEnd: Double?,
    val kind: String,
    val label: String?,
    val text: String?,
    val free: Boolean,
    val segmentIndex: Int? = null,
) {
    val id: String get() = "$start-$end-$kind"
}

/**
 * Field-by-field parsing keeps a malformed optional profile from blanking an
 * otherwise valid calendar day. The publisher bounds the profile to 512
 * blocks; the reader enforces the same bound, and -- like iOS, and like the
 * publisher's own rule -- refuses any block whose `end - start` is not its
 * `duration_s`.
 */
object CalendarWorkoutProfileDecoder {
    private const val maxBlocks = 512

    fun blocks(workout: JsonValue): List<CalendarWorkoutProfileBlock> {
        val values = workout.optArray("profile") ?: return emptyList()
        return values.take(maxBlocks).mapNotNull { value ->
            val start = finite(value, "start") ?: return@mapNotNull null
            val end = finite(value, "end") ?: return@mapNotNull null
            val duration = finite(value, "duration_s") ?: return@mapNotNull null
            val kind = (value.opt("kind") as? JsonValue.String)?.value?.takeUnless { it.isEmpty() }
                ?: return@mapNotNull null
            if (start < 0 || end < start || duration < 0 || end - start != duration) return@mapNotNull null
            CalendarWorkoutProfileBlock(
                start = start,
                end = end,
                durationS = duration,
                targetStart = finite(value, "target_start"),
                targetEnd = finite(value, "target_end"),
                kind = kind,
                label = (value.opt("label") as? JsonValue.String)?.value?.takeUnless { it.isEmpty() },
                text = (value.opt("text") as? JsonValue.String)?.value?.takeUnless { it.isEmpty() },
                free = (value.opt("free") as? JsonValue.Bool)?.value ?: false,
                segmentIndex = integer(value, "segment"),
            )
        }
    }

    private fun finite(value: JsonValue, key: String): Double? =
        (value.opt(key) as? JsonValue.Number)?.value?.takeIf { it.isFinite() }

    private fun integer(value: JsonValue, key: String): Int? {
        val number = finite(value, key) ?: return null
        return if (number == number.roundToLong().toDouble()) number.roundToInt() else null
    }
}

/**
 * The step list under the profile chart, in the desktop's words.
 *
 * Mirrors iOS `CalendarWorkoutFormatting`: same rounding (half-to-even, via
 * `Math.rint`, as Python and Swift both do it), same interval grouping
 * (consecutive blocks sharing an `intervals` label collapse into one line,
 * but never across a segment change), same chart scaling choices (watts when
 * an FTP exists, %FTP x 100 when it does not) and the same seven opacity
 * stops the desktop varies its fills by.
 */
object CalendarWorkoutFormatting {

    fun stepDescription(step: CalendarWorkoutProfileBlock, ftp: Double?): String {
        val duration = duration(step.durationS)
        if (step.free) return "$duration max effort \u2014 no target"
        val start = target(step.targetStart, ftp)
        val end = target(step.targetEnd, ftp)
        val percentStart = percent(step.targetStart)
        val percentEnd = percent(step.targetEnd)
        if (start == null || end == null) {
            return "$duration at ${percentStart ?: percentEnd ?: "unknown"} FTP"
        }
        if (start == end) {
            return if (percentStart != null) "$duration at ${start} W ($percentStart FTP)" else "$duration at $start W"
        }
        if (percentStart != null && percentEnd != null) {
            return "$duration at $start\u2013$end W ($percentStart\u2013$percentEnd FTP)"
        }
        return "$duration at $start\u2013$end W"
    }

    fun stepDescriptions(steps: List<CalendarWorkoutProfileBlock>, ftp: Double?): List<String> {
        val result = mutableListOf<String>()
        var index = 0
        while (index < steps.size) {
            val step = steps[index]
            val label = step.label
            if (step.kind != "intervals" || label.isNullOrEmpty()) {
                result.add(stepDescription(step, ftp))
                index += 1
                continue
            }
            val segmentIndex = step.segmentIndex
            val group = mutableListOf(step)
            var next = index + 1
            while (next < steps.size && steps[next].kind == "intervals" && steps[next].label == label) {
                val nextSegment = steps[next].segmentIndex
                if (segmentIndex != null && nextSegment != null && nextSegment != segmentIndex) break
                group.add(steps[next])
                next += 1
            }
            result.add(intervalDescription(label, group, ftp))
            index = next
        }
        return result
    }

    /** The coach's texts, consecutive duplicates collapsed. */
    fun coachingTexts(steps: List<CalendarWorkoutProfileBlock>): List<String> {
        val result = mutableListOf<String>()
        for (text in steps.mapNotNull { it.text }) {
            if (result.lastOrNull() != text) result.add(text)
        }
        return result
    }

    private fun intervalDescription(label: String, steps: List<CalendarWorkoutProfileBlock>, ftp: Double?): String {
        val watts = LinkedHashSet<Int>()
        val percentages = LinkedHashSet<String>()
        for (step in steps) {
            target(step.targetStart, ftp)?.let { watts.add(it) }
            percent(step.targetStart)?.let { percentages.add(it) }
        }
        var result = label
        if (watts.isNotEmpty()) result += " at " + watts.joinToString(" / ") { "$it W" }
        if (percentages.isNotEmpty()) result += " (" + percentages.joinToString(" / ") + " FTP)"
        return result
    }

    /** "45s" / "4 min" / "1m 30s" -- the desktop's step durations. */
    fun duration(seconds: Double): String {
        if (!seconds.isFinite() || seconds < 0) return "\u2014"
        val total = seconds.roundToLong()
        val minutes = total / 60
        val remainder = total % 60
        return when {
            minutes == 0L -> "${remainder}s"
            remainder == 0L -> "$minutes min"
            else -> "${minutes}m ${remainder}s"
        }
    }

    /** A %FTP fraction as whole watts; half-to-even at the half, as Python and Swift round. */
    fun target(fraction: Double?, ftp: Double?): Int? {
        if (fraction == null || !fraction.isFinite()) return null
        if (ftp == null || !ftp.isFinite() || ftp <= 0) return null
        return Math.rint(fraction * ftp).roundToInt()
    }

    /** A %FTP fraction in chart units: watts when an FTP exists, else %FTP x 100. */
    fun chartTarget(fraction: Double?, ftp: Double?): Double? {
        if (fraction == null || !fraction.isFinite()) return null
        if (ftp == null || !ftp.isFinite() || ftp <= 0) return fraction * 100
        return fraction * ftp
    }

    fun percent(fraction: Double?): String? {
        if (fraction == null || !fraction.isFinite()) return null
        return "${Math.rint(fraction * 100).roundToInt()}%"
    }

    /**
     * The desktop varies fill opacity by the same seven power zones. There is
     * no shared Android color set, so the chart uses the accent with these
     * same opacity stops rather than inventing a second hue palette (iOS's
     * comment applies verbatim).
     */
    fun zoneOpacity(fraction: Double): Double = when {
        fraction < 0.56 -> 0.08
        fraction < 0.76 -> 0.13
        fraction < 0.91 -> 0.18
        fraction < 1.06 -> 0.24
        fraction < 1.21 -> 0.31
        fraction < 1.51 -> 0.39
        else -> 0.48
    }
}
