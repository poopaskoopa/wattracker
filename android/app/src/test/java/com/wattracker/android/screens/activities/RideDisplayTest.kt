package com.wattracker.android.screens.activities

import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.json.JsonValue
import com.wattracker.android.ui.theme.Palette
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The ride display rules, ported from the iOS `RideDisplayTests`:
 * formatting, zone extraction, stream series selection, and the combined
 * chart's axes, clock and scaling. The clock and axis rules are covered only
 * by platform unit tests (a shared vector is not yet in `tests/vectors/`);
 * these keep the Kotlin half from drifting from the Swift half.
 */
class RideDisplayTest {

    // MARK: - Summary and formatting

    @Test
    fun aSummaryPreservesMissingHeartRateAndPower() {
        val summary = ActivitySummary(
            id = 7.0, startTime = null, durationS = 3661.0, distanceM = 1234.5,
            avgPower = null, avgHr = null, np = null, intensityFactor = null,
            tss = null, rpe = null,
        )
        val item = CloudItem(
            id = "activity-7", kind = CloudKind.Activity, revision = 1,
            deleted = false, payload = CloudPayload.Activity(summary),
        )
        val ride = RideSummary.from(item)
        assertNotNull(ride)
        assertEquals(7, ride!!.activityId)
        assertNull(ride.summary.avgHr)
        assertNull(ride.summary.avgPower)
        assertEquals("", ride.startedAt)
    }

    @Test
    fun aSummaryFallsBackToTheObjectIdForTheNumericId() {
        val summary = ActivitySummary(
            id = null, startTime = "2026-01-02T18:04:00", durationS = 3600.0,
            distanceM = null, avgPower = null, avgHr = null, np = null,
            intensityFactor = null, tss = null, rpe = null,
        )
        val item = CloudItem(
            id = "activity-17", kind = CloudKind.Activity, revision = 1,
            payload = CloudPayload.Activity(summary),
        )
        assertEquals(17, RideSummary.from(item)!!.activityId)
    }

    @Test
    fun aTombstoneIsNotARide() {
        val item = CloudItem(
            id = "activity-17", kind = CloudKind.Activity, revision = 1,
            deleted = true, payload = CloudPayload.Tombstone,
        )
        assertNull(RideSummary.from(item))
    }

    @Test
    fun formattingMatchesDesktopDurationRoundingAndPadding() {
        // Desktop formats durations in wattracker/races.py:627-637.
        assertEquals("05:09", RideFormatting.duration(309.0))
        assertEquals("1:00:00", RideFormatting.duration(3599.5))
        assertEquals("1:00:00", RideFormatting.duration(3600.5))
        assertEquals("1:00:01", RideFormatting.duration(3601.0))
    }

    @Test
    fun formattingHandlesDistanceAndMissingValues() {
        // The desktop detail card divides meters by 1,000 and formats one
        // decimal place.
        assertEquals("12.3 km", RideFormatting.distance(12345.0))
        assertEquals("—", RideFormatting.distance(null))
        assertEquals("0 W", RideFormatting.watts(0.0))
        assertEquals("—", RideFormatting.watts(null))
    }

    @Test
    fun dateParsesPublishedTimestampsAndFallsBackToTheRawText() {
        assertNotNull(RideFormatting.date("2026-01-02T18:04:00"))
        assertNotNull(RideFormatting.date("2026-01-02T18:04:00Z"))
        assertNull(RideFormatting.date(null))
        assertEquals("2026-01-02 18:04:00x", RideFormatting.date("2026-01-02T18:04:00x"))
    }

    // MARK: - Zones

    @Test
    fun zoneDurationUsesZoneFormatterNotRideCardPadding() {
        assertEquals("5:09", ZoneFormatting.duration(309.0))
        assertEquals("05:09", RideFormatting.duration(309.0))

        // Desktop rounds half to even: `int(round(seconds))` in Python 3.
        assertEquals("0:02", ZoneFormatting.duration(2.5))
        assertEquals("0:00", ZoneFormatting.duration(0.5))
        assertEquals("1:00", ZoneFormatting.duration(59.5))
        assertEquals("1:02:05", ZoneFormatting.duration(3725.0))
    }

    @Test
    fun zoneDurationPrefersPayloadStringAndFallsBackWhenMissing() {
        val zones = JsonValue.parse(
            """{"power":{"zones":[
                {"seconds":2.5,"duration":"0:03","percent":50},
                {"seconds":2.5,"percent":50}
            ]}}""",
        )
        val rows = ZoneGroup.extract(zones).firstOrNull()?.rows
            ?: throw AssertionError("expected the power group")
        assertEquals(listOf("0:03", "0:02"), rows.map { it.durationText })
        assertEquals("0:02", ZoneFormatting.duration(rows[0].seconds))
    }

    @Test
    fun zoneGroupsPreservePositiveRowsLabelsAndPercentages() {
        val zones = JsonValue.parse(
            """{"power":{"zones":[
                {"label":"Recovery","seconds":61,"percent":10.25},
                {"seconds":539,"percent":89.75},
                {"label":"empty","seconds":0,"percent":0}
            ]},
            "heart_rate":{"zones":[
                {"seconds":600,"percent":100}
            ]}}""",
        )
        val groups = ZoneGroup.extract(zones)
        assertEquals(listOf("power", "heart_rate"), groups.map { it.id })
        assertEquals(listOf(61.0, 539.0), groups[0].rows.map { it.seconds })
        assertEquals(listOf("Recovery", "Z2"), groups[0].rows.map { it.label })
        assertEquals(listOf(10.25, 89.75), groups[0].rows.map { it.percent })
        assertEquals(listOf(600.0), groups[1].rows.map { it.seconds })
        assertEquals(listOf("Z1"), groups[1].rows.map { it.label })
        assertTrue(ZoneGroup.extract(null).isEmpty())
    }

    // MARK: - Stream series

    @Test
    fun streamsDropGapsAndUseTimeChannelWhenPresent() {
        val channels = ActivityStreams.Channels(
            time = null,
            power = listOf(0.0, null, Double.POSITIVE_INFINITY),
            heartrate = listOf(null, null),
            cadence = listOf(null, null),
            altitude = null,
        )
        val series = StreamSeries.all(ActivityStreams(streams = channels))
        assertEquals(1, series.size)
        assertEquals("power", series[0].id)
        assertEquals(listOf(0.0), series[0].points.map { it.time })
        assertEquals(listOf(0.0), series[0].points.map { it.value })
        // Equivalent to the desktop `have` map: a channel with no finite
        // sample is not rendered.
        assertFalse(series.any { it.id == "heart-rate" })
        assertFalse(series.any { it.id == "cadence" })
        assertTrue(
            StreamSeries.all(
                ActivityStreams(
                    streams = ActivityStreams.Channels(null, null, null, null, null),
                ),
            ).isEmpty(),
        )

        val timedChannels = ActivityStreams.Channels(
            time = listOf(12.5, 18.25, 24.75),
            power = listOf(100.0, null, 300.0),
            heartrate = null, cadence = null, altitude = null,
        )
        val timedSeries = StreamSeries.all(ActivityStreams(streams = timedChannels))
        assertEquals(listOf(12.5, 24.75), timedSeries[0].points.map { it.time })
        assertEquals(listOf(100.0, 300.0), timedSeries[0].points.map { it.value })
    }

    @Test
    fun streamSeriesPreserveActivityChartOrderAndPalette() {
        val channels = ActivityStreams.Channels(
            time = listOf(0.0, 1.0),
            power = listOf(100.0, 101.0),
            heartrate = listOf(140.0, 141.0),
            cadence = listOf(88.0, 89.0),
            altitude = null,
        )
        val series = StreamSeries.all(ActivityStreams(streams = channels))
        assertEquals(listOf("power", "heart-rate", "cadence"), series.map { it.id })
        assertEquals(
            listOf("Power (W)", "Heart rate (bpm)", "Cadence (rpm)"),
            series.map { it.title },
        )
        assertEquals(listOf(Palette.accent, Palette.hr, Palette.ok), series.map { it.color })
    }

    // MARK: - The combined chart

    @Test
    fun combinedChartUsesSharedAxesAndLayersAltitudeBehind() {
        val channels = ActivityStreams.Channels(
            time = listOf(0.0, 60.0, 120.0),
            power = listOf(0.0, 100.0, 200.0),
            heartrate = listOf(100.0, 150.0, 200.0),
            cadence = listOf(50.0, 60.0, 70.0),
            altitude = listOf(50.0, 60.0, 70.0),
        )
        val combined = CombinedChartSeries.build(ActivityStreams(streams = channels))
        assertEquals(
            listOf("altitude", "power", "heart-rate", "cadence"),
            combined.map { it.id },
        )
        assertTrue(combined[0].isBackground)
        assertEquals(listOf(0.0, 0.5, 1.0), combined[0].points.map { it.scaled }) // auto min...max
        assertEquals(listOf(0.0, 0.5, 1.0), combined[1].points.map { it.scaled }) // power axis 0...200
        // HR and cadence share one 0...200 scale, so cadence stays low.
        assertEquals(listOf(0.5, 0.75, 1.0), combined[2].points.map { it.scaled })
        assertEquals(listOf(0.25, 0.3, 0.35), combined[3].points.map { it.scaled })
        assertEquals(listOf(0.0, 1.0, 2.0), combined[1].points.map { it.minutes })
        val all = StreamSeries.all(ActivityStreams(streams = channels))
        assertEquals("W", CombinedChartSeries.axis(CombinedChartSeries.Side.power, all)?.title)
        assertEquals("bpm / rpm", CombinedChartSeries.axis(CombinedChartSeries.Side.shared, all)?.title)
        assertEquals(200.0, CombinedChartSeries.axis(CombinedChartSeries.Side.shared, all)!!.upper, 1e-9)
        assertEquals(1.0, CombinedChartSeries.axis(CombinedChartSeries.Side.shared, all)!!.scaledTicks.last(), 1e-9)
    }

    @Test
    fun eightySixMinuteRideSpansTrueMinutesWithoutTimeChannel() {
        // Regression: the cloud time channel is all nil, so x fell back to the
        // downsampled point index / 60 and an 86-minute ride read 0...25.
        val n = 1_500
        val duration = 86.0 * 60
        val ramp = (0 until n).map { (it % 300).toDouble() }
        val channels = ActivityStreams.Channels(
            time = List(n) { null },
            power = ramp,
            heartrate = ramp,
            cadence = null,
            altitude = null,
        )
        val payload = ActivityStreams(streams = channels)
        val series = CombinedChartSeries.build(payload, durationSeconds = duration)
        val xs = series[0].points.map { it.minutes }
        assertEquals(0.0, xs.first(), 1e-9)
        assertEquals(86.0, xs.last(), 1e-9)
        assertEquals(86.0, CombinedChartSeries.extent(series), 1e-9)
        assertEquals(listOf(0.0, 15.0, 30.0, 45.0, 60.0, 75.0), ChartTicks.minutes(86.0))

        // 1 Hz, un-downsampled samples agree with the 1 Hz fallback.
        val hz = ActivityStreams(
            streams = ActivityStreams.Channels(
                time = null,
                power = List(5_161) { 100.0 },
                heartrate = null, cadence = null, altitude = null,
            ),
        )
        assertEquals(5_160.0 / 60, CombinedChartSeries.extent(CombinedChartSeries.build(hz)), 1e-9)
        assertEquals(86.0, CombinedChartSeries.extent(CombinedChartSeries.build(hz, durationSeconds = 5_160.0)), 1e-9)
    }

    @Test
    fun chartAxisEdgeCases() {
        assertEquals(
            listOf(0.0, 100.0, 200.0, 300.0, 400.0),
            ChartTicks.nice(lower = 0.0, upper = 312.0, target = 5).ticks,
        )
        assertTrue(ChartTicks.nice(lower = 0.0, upper = 0.0, target = 5).upper > 0)
        val flat = ChartAxis.zeroBased(listOf(0.0, 0.0), "W")
        assertTrue(flat.upper > flat.lower)
        val neg = ChartAxis.zeroBased(listOf(-20.0, 100.0), "W")
        assertTrue(neg.lower <= -20.0)
        assertTrue(neg.upper >= 100.0)

        // Single sample: no divide-by-zero, finite output.
        val one = ActivityStreams(
            streams = ActivityStreams.Channels(
                time = null, power = listOf(150.0),
                heartrate = null, cadence = null, altitude = listOf(10.0),
            ),
        )
        val built = CombinedChartSeries.build(one, durationSeconds = 3_600.0)
        built.flatMap { it.points }.forEach {
            assertTrue(it.scaled.isFinite())
            assertTrue(it.minutes.isFinite())
        }
        assertEquals(listOf(0.0), ChartTicks.minutes(0.0))
    }

    // MARK: - Tick labels

    @Test
    fun tickLabelsArePlainNumbersWithOneDecimalOnlyWhenNotWhole() {
        assertEquals("0", chartTickLabel(0.0))
        assertEquals("100", chartTickLabel(100.0))
        assertEquals("42.5", chartTickLabel(42.5))
        assertEquals("75", chartTickLabel(75.0))
    }
}
