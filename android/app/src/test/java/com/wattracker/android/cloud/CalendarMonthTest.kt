package com.wattracker.android.cloud

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.util.Locale

class CalendarMonthTest {

    @Test
    fun gridIsFullMondayWeeksCoveringTheMonth() {
        // September 2026: the 1st is a Tuesday, so the first row leads with
        // Monday 2026-08-31, and the last runs into October. Five rows.
        val grid = CalendarMonth(2026, 9).gridDates()
        assertEquals(35, grid.size)
        assertEquals("2026-08-31", grid.first().isoDate)
        assertFalseCell(grid.first())
        assertEquals("2026-10-04", grid.last().isoDate)
        // Cells are row by row, seven per row, dates strictly ascending.
        for (row in grid.chunked(7)) {
            assertEquals(7, row.size)
            assertEquals(
                row.map { it.isoDate },
                row.map { it.isoDate }.sorted(),
            )
        }
        assertEquals(30, grid.count { it.isInMonth })
        assertEquals(1, grid.indexOfFirst { it.isInMonth })
    }

    @Test
    fun leadingEmptyCellsMatchTheFirstWeekday() {
        // 2026-10-01 is a Thursday: three cells before it.
        assertEquals(3, CalendarMonth(2026, 10).leadingEmptyDays)
        // 2026-11-01 is a Sunday: the grid leads with the Monday of October.
        assertEquals(6, CalendarMonth(2026, 11).leadingEmptyDays)
        // 2027-02-01 is a Monday: the first week is whole.
        val grid = CalendarMonth(2027, 2).gridDates()
        assertEquals(0, CalendarMonth(2027, 2).leadingEmptyDays)
        assertEquals("2027-02-01", grid.first().isoDate)
    }

    @Test
    fun leapFebruaryKeepsTheWeeksWhole() {
        val february = CalendarMonth(2024, 2)
        assertEquals(29, february.dayCount)
        // 2024-02-01 is a Thursday: 3 leading, 29 days, 3 trailing = 35 cells, 5 rows.
        val grid = february.gridDates()
        assertEquals(35, grid.size)
        assertEquals("2024-01-29", grid.first().isoDate)
        assertEquals("2024-03-03", grid.last().isoDate)
        assertEquals(29, grid.count { it.isInMonth })
    }

    @Test
    fun monthArithmeticWrapsLikeTheCalendar() {
        assertEquals(CalendarMonth(2027, 1), CalendarMonth(2026, 12).addingMonths(1))
        assertEquals(CalendarMonth(2025, 12), CalendarMonth(2026, 1).addingMonths(-1))
        assertEquals(CalendarMonth(2026, 9), CalendarMonth(2026, 9).addingMonths(-12).addingMonths(12))
        assertEquals(CalendarMonth(2025, 2), CalendarMonth(2026, 2).addingMonths(-12))
    }

    @Test
    fun isoDateRoundTripsInsideTheMonth() {
        assertEquals("2026-09-01", CalendarMonth(2026, 9).isoDate(1))
        assertEquals("2026-09-30", CalendarMonth(2026, 9).isoDate(30))
    }

    @Test
    fun fromIsoDateIsStrictAboutRealDates() {
        assertEquals(CalendarMonth.fromISODate("2026-09-01"), CalendarMonth(2026, 9))
        // A February that never happened.
        assertNull(CalendarMonth.fromISODate("2025-02-29"))
        // A month that never happened.
        assertNull(CalendarMonth.fromISODate("2026-13-01"))
        // Not the ten characters of a date.
        assertNull(CalendarMonth.fromISODate("2026-1-1"))
        assertNull(CalendarMonth.fromISODate("2026-09-01T00:00:00"))
        assertNull(CalendarMonth.fromISODate(null))
    }

    /**
     * #456 review, finding 4. The month heading's order belongs to the locale,
     * not to this class: the caller passes the platform's month-year skeleton,
     * which is Android's answer to the ICU template iOS asks
     * (`setLocalizedDateFormatFromTemplate("LLLLyyyy")`), and a year-first
     * locale gets a year-first title. A fixed "LLLL yyyy" could not say this.
     */
    @Test
    fun theMonthTitleFollowsTheSkeletonItIsGiven() {
        val september = CalendarMonth(2026, 9)
        assertEquals("September 2026", september.title(Locale.ENGLISH, "LLLL yyyy"))
        assertEquals("septembre 2026", september.title(Locale.FRENCH, "MMMM yyyy"))
        assertEquals("2026年9月", september.title(Locale.JAPAN, "yyyy年M月"))
        assertEquals("2026. szeptember", september.title(Locale.forLanguageTag("hu"), "yyyy. MMMM"))
        assertTrue(
            "the year-first locales read year-first, which is the finding",
            september.title(Locale.JAPAN, "yyyy年M月").startsWith("2026"),
        )
    }

    @Test
    fun aCallerWithNoSkeletonStillGetsTheMonthNamed() {
        assertEquals("September 2026", CalendarMonth(2026, 9).title(Locale.ENGLISH))
        assertEquals(
            "September 2026",
            CalendarMonth(2026, 9).title(Locale.ENGLISH, CalendarMonth.FALLBACK_TITLE_PATTERN),
        )
    }

    /**
     * ICU is free to answer with a letter or a calendar this ISO, date-only
     * model has no field for. A header that degrades to the old order beats a
     * screen that throws on a device, so the fallback is a rule, not a hope.
     */
    @Test
    fun anUnusableSkeletonFallsBackInsteadOfThrowing() {
        // A pattern whose field this date-only arithmetic has no answer for:
        // an offset, and a time of day. Neither a month nor a year.
        assertEquals("September 2026", CalendarMonth(2026, 9).title(Locale.ENGLISH, "yyyy MMMM XXX"))
        assertEquals("September 2026", CalendarMonth(2026, 9).title(Locale.ENGLISH, "HH:mm yyyy MMMM"))
    }

    @Test
    fun currentReadsTheMonthFromTheEpochInTheDeviceZone() {
        // "Current" is the rider's month: the device zone reads the instant,
        // and the test reads it the same way -- the claim is that the two
        // readings cannot differ.
        val millis = Instant.parse("2026-06-15T12:00:00Z").toEpochMilli()
        val expected = java.time.ZonedDateTime.ofInstant(Instant.ofEpochMilli(millis), java.time.ZoneId.systemDefault())
        val withZone = CalendarMonth.current(millis)
        assertEquals(expected.year, withZone.year)
        assertEquals(expected.monthValue, withZone.month)
    }

    @Test
    fun idIsTheZeroPaddedSortForm() {
        assertEquals("2026-09", CalendarMonth(2026, 9).id)
        assertEquals("0999-01", CalendarMonth(999, 1).id)
    }

    @Test
    fun monthRejectsANumberItCannotName() {
        assertTrue(expectFailure { CalendarMonth(2026, 0) })
        assertTrue(expectFailure { CalendarMonth(2026, 13) })
    }

    private inline fun expectFailure(block: () -> Unit): Boolean = try {
        block()
        false
    } catch (_: IllegalArgumentException) {
        true
    }

    private fun assertFalseCell(cell: CalendarMonth.GridDay) {
        org.junit.Assert.assertFalse(cell.isInMonth)
    }
}
