package com.wattracker.android.cloud

import java.time.Instant
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.util.Locale

/**
 * A Gregorian month, addressed by year and number -- the calendar screen's
 * unit of navigation, and the seam's month parameter for the calendar route.
 *
 * Calendar objects carry rider-local, date-only values (the desktop already
 * bucketed in the rider's timezone). Keeping a month as year/month components
 * and doing every step of its grid arithmetic on [LocalDate] -- no instants,
 * no zones -- is what keeps a phone's timezone from ever moving a cell across
 * a day boundary. This is the same posture iOS's `CalendarMonth` takes with a
 * UTC Gregorian calendar; `java.time` is that calendar by construction.
 *
 * The grid itself is pinned by the shared vector `tests/vectors/calendar_grid_v1.json`,
 * generated from the desktop's own `calendar.monthdatescalendar` -- the rule
 * is the desktop's, not this file's, and [gridDates] must agree with it.
 */
data class CalendarMonth(val year: Int, val month: Int) : Comparable<CalendarMonth> {

    init {
        require(month in 1..12) { "month out of range: $month" }
    }

    /** The month's cache-key / sort form, zero padded. */
    val id: String get() = String.format(Locale.US, "%04d-%02d", year, month)

    /** The 1st of the month. Date-only arithmetic hangs off this. */
    val firstDate: LocalDate get() = LocalDate.of(year, month, 1)

    /** Days in the month, leap years included. */
    val dayCount: Int get() = firstDate.lengthOfMonth()

    /** Empty cells before the 1st when weeks begin on Monday (0..6). */
    val leadingEmptyDays: Int get() = firstDate.dayOfWeek.value - 1 // DayOfWeek: Mon=1

    /** One cell of the month grid: a real date, which may belong to an adjacent month. */
    data class GridDay(val isoDate: String, val day: Int, val isInMonth: Boolean)

    /**
     * Full Monday-to-Sunday weeks covering the month, as the desktop calendar
     * lays them out (`calendar.monthdatescalendar`, Monday first): the first
     * row starts on the Monday on or before the 1st, the last row ends on the
     * Sunday on or after the last day, and the cells outside the month are the
     * adjacent months' real dates. Row by row, seven per row.
     */
    fun gridDates(): List<GridDay> {
        val leading = leadingEmptyDays
        val trailing = (7 - (leading + dayCount) % 7) % 7
        val start = firstDate.minusDays(leading.toLong())
        return (0 until leading + dayCount + trailing).map { offset ->
            val date = start.plusDays(offset.toLong())
            GridDay(
                isoDate = ISO_DATE.format(date),
                day = date.dayOfMonth,
                isInMonth = date.year == year && date.monthValue == month,
            )
        }
    }

    /**
     * The month named in [locale] ("September 2026"). Locale-free arithmetic;
     * only this reads it.
     *
     * iOS names the same month from a localized template
     * (`setLocalizedDateFormatFromTemplate("LLLLyyyy")`), which puts the year
     * and the month in the order that locale wants. `java.time` has no
     * month-year template to ask, and the Android call that does
     * (`android.text.format.DateFormat.getBestDateTimePattern`) is a framework
     * API this cloud-plane class cannot reach from its JVM tests. So the order
     * here is fixed -- "LLLL yyyy", the standalone month form -- and the
     * ordering a locale like ja would want is recorded in
     * `docs/mobile-parity.md` rather than papered over with hand-written
     * per-language patterns.
     */
    fun title(locale: Locale = Locale.getDefault()): String =
        DateTimeFormatter.ofPattern("LLLL yyyy", locale).format(firstDate)

    /** The month [offset] months away, with the wrap the calendar does. */
    fun addingMonths(offset: Int): CalendarMonth {
        val moved = YearMonth.of(year, month).plusMonths(offset.toLong())
        return CalendarMonth(moved.year, moved.monthValue)
    }

    /** The ISO date of a day inside this month. */
    fun isoDate(day: Int): String {
        require(day in 1..dayCount) { "day out of range: $day" }
        return ISO_DATE.format(firstDate.withDayOfMonth(day))
    }

    override fun compareTo(other: CalendarMonth): Int {
        if (year != other.year) return year.compareTo(other.year)
        return month.compareTo(other.month)
    }

    companion object {
        private val ISO_DATE: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd", Locale.US)

        /** The month the device's clock sits in, in its own zone -- the rider's "now". */
        fun current(epochMillis: Long = System.currentTimeMillis()): CalendarMonth {
            val today = Instant.ofEpochMilli(epochMillis).atZone(ZoneId.systemDefault()).toLocalDate()
            return CalendarMonth(today.year, today.monthValue)
        }

        /**
         * [value] as a month when it is a well-formed, real `yyyy-MM-dd` date, else null.
         *
         * Strict on purpose: a malformed date on the wire must drop out of the
         * month it would otherwise land in, not join an arbitrary one. The
         * strictness is [isoDateOrNull]'s -- including the round trip that
         * rejects what a lenient reader would otherwise accept (out-of-range
         * months, `2025-02-29`, a short year) -- because every published
         * date-only value is read through that one door; this only names the
         * month the date lands in.
         */
        fun fromISODate(value: String?): CalendarMonth? {
            val date = isoDateOrNull(value) ?: return null
            return CalendarMonth(date.year, date.monthValue)
        }
    }
}

/**
 * A published date-only value (`calendar_day.date`, `volume_week.week_start`)
 * read as a [LocalDate], or null when it is not a real `yyyy-MM-dd` date.
 *
 * Same strictness as [CalendarMonth.fromISODate] -- and the same reason: the
 * server's dates are rider-local and already bucketed. A value this client
 * cannot read exactly is dropped, never guessed at, and never re-bucketed.
 */
fun isoDateOrNull(value: String?): LocalDate? {
    if (value == null || value.length != 10 || value[4] != '-' || value[7] != '-') return null
    val date = try {
        LocalDate.parse(value, DateTimeFormatter.ISO_LOCAL_DATE)
    } catch (_: DateTimeParseException) {
        return null
    }
    return if (DateTimeFormatter.ISO_LOCAL_DATE.format(date) == value) date else null
}
