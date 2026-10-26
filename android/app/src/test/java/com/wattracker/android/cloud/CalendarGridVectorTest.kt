package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.opt
import com.wattracker.android.json.optArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * The Kotlin half of the shared calendar-grid vector.
 *
 * `tests/vectors/calendar_grid_v1.json` is generated from the desktop's own
 * `_calendar_data` (`calendar.monthdatescalendar`) and read, unmodified, by
 * the Python suite, the Swift `BehaviourVectorTests`, and here -- which is
 * what the vector's own description promises: "Android consumes it once it
 * has a calendar grid (#198)". The rule is the desktop's, so the clients are
 * checked against it rather than against two transcriptions of it; the iOS
 * grid once put 1 October 2026 on a Tuesday (#431), and this file is why
 * that cannot silently happen twice.
 */
class CalendarGridVectorTest {

    @Test
    fun theGridMatchesTheDesktopForEveryPinnedMonth() {
        val file = TestVectors.parse("calendar_grid_v1.json")
        assertEquals(1.0, (file.opt("version") as? JsonValue.Number)?.value ?: 0.0, 1e-9)
        val months = file.optArray("months") ?: emptyList()
        assertFalse("the vector carries no months", months.isEmpty())
        for (entryObj in months) {
            val year = (entryObj.opt("year") as? JsonValue.Number)?.value?.toInt()
                ?: throw AssertionError("month case without a year")
            val month = (entryObj.opt("month") as? JsonValue.Number)?.value?.toInt()
                ?: throw AssertionError("month case without a month")
            val expected = (entryObj.optArray("cells") ?: emptyList()).map { cell ->
                CalendarMonth.GridDay(
                    isoDate = (cell.opt("iso_date") as? JsonValue.String)?.value
                        ?: throw AssertionError("cell without iso_date"),
                    day = (cell.opt("day") as? JsonValue.Number)?.value?.toInt()
                        ?: throw AssertionError("cell without day"),
                    isInMonth = (cell.opt("in_month") as? JsonValue.Bool)?.value
                        ?: throw AssertionError("cell without in_month"),
                )
            }
            assertFalse("month %04d-%02d carries no cells".format(year, month), expected.isEmpty())
            assertEquals(
                "%04d-%02d".format(year, month),
                expected,
                CalendarMonth(year, month).gridDates(),
            )
        }
    }
}
