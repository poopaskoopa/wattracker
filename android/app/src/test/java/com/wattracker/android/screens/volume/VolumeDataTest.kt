package com.wattracker.android.screens.volume

import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.VolumeWeek
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate

class VolumeDataTest {

    private fun weekItem(weekStart: String?, hours: Double? = 1.0, tss: Double? = 50.0): CloudItem = CloudItem(
        id = "volume-week-$weekStart",
        kind = CloudKind.VolumeWeek,
        revision = 1,
        payload = CloudPayload.VolumeWeek(VolumeWeek(weekStart, hours, tss, 30.0, 500.0)),
    )

    private fun snapshot(items: List<CloudItem>): CloudSnapshot =
        CloudSnapshot(CloudRoute.Volume, 1, items, CloudSnapshot.Source.network, 3000L)

    @Test
    fun weeksSortAndUnreadableWeekStartsAreDropped() {
        val data = VolumeData(
            snapshot(
                listOf(
                    weekItem("2026-05-11"),
                    weekItem("not-a-week"),
                    weekItem("2026-05-04"),
                ),
            ),
        )
        assertEquals(listOf("2026-05-04", "2026-05-11"), data.weeks.map { it.weekStart })
    }

    @Test
    fun gapsBecomeZeroWeeksOnce() {
        val data = VolumeData(
            snapshot(
                listOf(
                    weekItem("2026-05-04"),
                    // The week of the 11th published nothing: the server
                    // omits it, and the chart must still space it.
                    weekItem("2026-05-18"),
                ),
            ),
        )
        assertEquals(
            listOf("2026-05-04", "2026-05-11", "2026-05-18"),
            data.denseWeeks.map { it.weekStart },
        )
        assertEquals(0.0, data.denseWeeks[1].hours!!, 1e-9)
    }

    @Test
    fun duplicateWeeksKeepTheLast() {
        val data = VolumeData(
            snapshot(
                listOf(
                    weekItem("2026-05-04", hours = 1.0),
                    weekItem("2026-05-04", hours = 9.0),
                ),
            ),
        )
        assertEquals(1, data.weeks.size)
        assertEquals(9.0, data.weeks.single().hours!!, 1e-9)
    }

    @Test
    fun tombstonesAreAbsent() {
        val deleted = CloudItem("volume-week-2026-05-04", CloudKind.VolumeWeek, 1, deleted = true, payload = CloudPayload.Tombstone)
        val data = VolumeData(snapshot(listOf(deleted, weekItem("2026-05-11"))))
        assertEquals(1, data.weeks.size)
    }

    @Test
    fun lastNWindowsTakeTheTail() {
        val weeks = (0 until 20).map { weekItem(monday(it)) }
        val data = VolumeData(snapshot(weeks))
        assertEquals(4, data.weeksFor(VolumeRange.Last4).size)
        assertEquals(12, data.weeksFor(VolumeRange.Last12).size)
        // Twenty weeks from 2026-05-04: the last four start at the 17th.
        assertEquals("2026-08-24", data.weeksFor(VolumeRange.Last4).first().weekStart)
    }

    @Test
    fun yearToDateKeepsTheBucketsWhoseMondayIsInTheYear() {
        // Volume is published as Monday buckets, and a bucket belongs to the
        // year of its Monday -- the desktop's own rule, ported with iOS.
        // 2025-12-29 is a 2025 bucket even though most of its week is 2026;
        // keeping it in a 2026 YTD would be a client-side re-bucket.
        val data = VolumeData(
            snapshot(
                listOf(
                    weekItem("2025-12-29"),
                    weekItem("2026-01-05"),
                    weekItem("2026-06-01"),
                ),
            ),
        )
        // The dense fill spaces the year, so the assertion is about the
        // edges: the 2025 bucket is out, 2026's own Mondays are in.
        val ytd = data.weeksFor(VolumeRange.YearToDate, referenceDate = LocalDate.of(2026, 6, 15))
        assertEquals("2026-01-05", ytd.first().weekStart)
        assertEquals("2026-06-01", ytd.last().weekStart)
        assertTrue(ytd.none { (it.weekStart ?: "").startsWith("2025") })
        val ytd2025 = data.weeksFor(VolumeRange.YearToDate, referenceDate = LocalDate.of(2025, 12, 30))
        assertEquals(listOf("2025-12-29"), ytd2025.map { it.weekStart })
    }

    @Test
    fun previousWindowNeedsTwoCompleteWindows() {
        val twelve = (0 until 12).map { weekItem(monday(it)) }
        val justEnough = (0 until 24).map { weekItem(monday(it)) }
        val short = VolumeData(snapshot(twelve))
        assertTrue(short.previousWeeks(VolumeRange.Last12).isEmpty())
        val long = VolumeData(snapshot(justEnough))
        assertEquals(12, long.previousWeeks(VolumeRange.Last12).size)
        assertTrue(VolumeData(snapshot(emptyList())).previousWeeks(VolumeRange.YearToDate).isEmpty())
    }

    @Test
    fun changeIsNothingOutOfZero() {
        val weeks = (0 until 24).map { weekItem(monday(it), hours = if (it < 12) 0.0 else 10.0) }
        val data = VolumeData(snapshot(weeks))
        // Previous 12 weeks are all zeros: a change out of zero is not a
        // percentage, and showing one would be a lie.
        assertNull(data.change(VolumeMetric.Hours, VolumeRange.Last12))

        val nonzero = (0 until 24).map { weekItem(monday(it), hours = if (it < 12) 5.0 else 10.0) }
        val data2 = VolumeData(snapshot(nonzero))
        val change = data2.change(VolumeMetric.Hours, VolumeRange.Last12)
        assertNotNull(change)
        assertEquals(1.0, change!!, 1e-9)
    }

    @Test
    fun totalsSumTheWindow() {
        val data = VolumeData(snapshot((0 until 4).map { weekItem(monday(it), hours = 2.0, tss = 60.0) }))
        val summary = data.summaryFor(VolumeRange.Last4)
        assertEquals(8.0, summary.hours, 1e-9)
        assertEquals(240.0, summary.tss, 1e-9)
        assertEquals(120.0, summary.distanceKm, 1e-9)
        assertEquals(2000.0, summary.calories, 1e-9)
    }

    @Test
    fun formattingVoicesMatchTheScreens() {
        assertEquals("8.0 h", VolumeFormat.value(8.0, VolumeMetric.Hours))
        assertEquals("240 TSS", VolumeFormat.value(240.0, VolumeMetric.Tss))
        assertEquals("+100%", VolumeFormat.percent(1.0))
        assertEquals("-25%", VolumeFormat.percent(-0.25))
        // No sign for a window that did not move. The screen pairs the number
        // with an arrow, and the desktop prints a flat marker and a bare "0%"
        // -- "+0%" under an up arrow reads as growth.
        assertEquals("0%", VolumeFormat.percent(0.0))
    }

    /** Monday of the n-th week of May 2026, for synthetic ranges. */
    private fun monday(index: Int): String {
        val first = LocalDate.of(2026, 5, 4)
        return first.plusWeeks(index.toLong()).toString()
    }
}
