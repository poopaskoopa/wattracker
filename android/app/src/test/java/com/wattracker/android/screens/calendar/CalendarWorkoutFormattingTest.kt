package com.wattracker.android.screens.calendar

import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.json.JsonValue
import com.wattracker.android.cloud.TestVectors
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The step list under the profile chart -- iOS `CalendarWorkoutFormatting`
 * ported case for case, including the rounding posture (half-to-even, as
 * Python's `round()` and Swift's `.toNearestOrEven` do it) so all three
 * clients name the same block the same watts.
 */
class CalendarWorkoutFormattingTest {

    private fun block(
        start: Double,
        end: Double,
        targetStart: Double?,
        targetEnd: Double?,
        kind: String = "steady",
        label: String? = null,
        text: String? = null,
        free: Boolean = false,
        segment: Int? = null,
    ): JsonValue {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["start"] = JsonValue.Number(start)
        fields["end"] = JsonValue.Number(end)
        fields["duration_s"] = JsonValue.Number(end - start)
        fields["kind"] = JsonValue.String(kind)
        targetStart?.let { fields["target_start"] = JsonValue.Number(it) }
        targetEnd?.let { fields["target_end"] = JsonValue.Number(it) }
        label?.let { fields["label"] = JsonValue.String(it) }
        text?.let { fields["text"] = JsonValue.String(it) }
        if (free) fields["free"] = JsonValue.Bool(true)
        segment?.let { fields["segment"] = JsonValue.Number(it.toDouble()) }
        return JsonValue.Object(fields)
    }

    private fun workoutJson(vararg blocks: JsonValue): JsonValue =
        JsonValue.Object(linkedMapOf("profile" to JsonValue.Array(blocks.toList())))

    @Test
    fun decoderKeepsThePublishersOwnValidation() {
        val workout = workoutJson(
            block(0.0, 300.0, 0.8, 0.8),
            // `end - start` not equal to `duration_s`: not a block, dropped.
            JsonValue.Object(
                linkedMapOf(
                    "start" to JsonValue.Number(300.0),
                    "end" to JsonValue.Number(600.0),
                    "duration_s" to JsonValue.Number(400.0),
                    "kind" to JsonValue.String("steady"),
                ),
            ),
            // No kind: dropped, like iOS.
            JsonValue.Object(
                linkedMapOf(
                    "start" to JsonValue.Number(0.0),
                    "end" to JsonValue.Number(60.0),
                    "duration_s" to JsonValue.Number(60.0),
                ),
            ),
        )
        val blocks = CalendarWorkoutProfileDecoder.blocks(workout)
        assertEquals(1, blocks.size)
        assertEquals(0.8, blocks.single().targetStart!!, 1e-9)
    }

    @Test
    fun decoderHonoursThe512BlockBound() {
        val many = workoutJson(*Array(600) { i -> block(i.toDouble() * 10, i.toDouble() * 10 + 10, 0.9, 0.9) })
        assertEquals(512, CalendarWorkoutProfileDecoder.blocks(many).size)
    }

    @Test
    fun freeBlocksKeepTheirFlag() {
        val blocks = CalendarWorkoutProfileDecoder.blocks(workoutJson(block(0.0, 45.0, null, null, free = true)))
        assertTrue(blocks.single().free)
    }

    @Test
    fun stepDescriptionsMatchIosWording() {
        assertEquals(
            "45s max effort \u2014 no target",
            CalendarWorkoutFormatting.stepDescription(
                CalendarWorkoutProfileBlock(0.0, 45.0, 45.0, null, null, "steady", null, null, free = true),
                ftp = 250.0,
            ),
        )
        assertEquals(
            "5 min at 200 W (80% FTP)",
            CalendarWorkoutFormatting.stepDescription(
                CalendarWorkoutProfileBlock(0.0, 300.0, 300.0, 0.8, 0.8, "steady", null, null, free = false),
                ftp = 250.0,
            ),
        )
        assertEquals(
            "2m 30s at 200\u2013250 W (80%\u2013100% FTP)",
            CalendarWorkoutFormatting.stepDescription(
                CalendarWorkoutProfileBlock(0.0, 150.0, 150.0, 0.8, 1.0, "ramp", null, null, free = false),
                ftp = 250.0,
            ),
        )
        // No FTP in sight: the step speaks in percent only.
        assertEquals(
            "5 min at 80% FTP",
            CalendarWorkoutFormatting.stepDescription(
                CalendarWorkoutProfileBlock(0.0, 300.0, 300.0, 0.8, 0.8, "steady", null, null, free = false),
                ftp = null,
            ),
        )
        assertEquals(
            "1 min at unknown FTP",
            CalendarWorkoutFormatting.stepDescription(
                CalendarWorkoutProfileBlock(0.0, 60.0, 60.0, null, null, "steady", null, null, free = false),
                ftp = 250.0,
            ),
        )
    }

    @Test
    fun intervalBlocksSharingALabelCollapseIntoOneLine() {
        val steps = CalendarWorkoutProfileDecoder.blocks(
            workoutJson(
                block(0.0, 300.0, 0.9, 0.9, kind = "intervals", label = "5x4min"),
                block(300.0, 600.0, 0.5, 0.5, kind = "intervals", label = "5x4min"),
                block(600.0, 900.0, 0.9, 0.9, kind = "intervals", label = "5x4min"),
            ),
        )
        val lines = CalendarWorkoutFormatting.stepDescriptions(steps, ftp = 250.0)
        assertEquals(1, lines.size)
        assertEquals("5x4min at 225 W / 125 W (90% / 50% FTP)", lines.single())
    }

    @Test
    fun segmentChangesSplitTheCollapse() {
        val steps = CalendarWorkoutProfileDecoder.blocks(
            workoutJson(
                block(0.0, 300.0, 0.9, 0.9, kind = "intervals", label = "reps", segment = 1),
                block(300.0, 600.0, 0.9, 0.9, kind = "intervals", label = "reps", segment = 2),
            ),
        )
        assertEquals(2, CalendarWorkoutFormatting.stepDescriptions(steps, ftp = 250.0).size)
    }

    @Test
    fun coachingTextsCollapseConsecutiveDuplicates() {
        val steps = CalendarWorkoutProfileDecoder.blocks(
            workoutJson(
                block(0.0, 60.0, 0.5, 0.5, text = "Spin up"),
                block(60.0, 120.0, 0.5, 0.5, text = "Spin up"),
                block(120.0, 180.0, 0.5, 0.5, text = "Hold the cadence"),
            ),
        )
        assertEquals(listOf("Spin up", "Hold the cadence"), CalendarWorkoutFormatting.coachingTexts(steps))
    }

    @Test
    fun roundingIsHalfToEvenLikePythonAndSwift() {
        // 0.5 * 5 = 2.5 -> 2, not 3. `Math.rint` is half-to-even, matching
        // `round()` in Python and `.rounded(.toNearestOrEven)` in Swift.
        assertEquals(2, CalendarWorkoutFormatting.target(0.5, 5.0))
        assertEquals(200, CalendarWorkoutFormatting.target(0.8, 250.0))
        assertNullish(CalendarWorkoutFormatting.target(0.8, 0.0))
        assertNullish(CalendarWorkoutFormatting.target(null, 250.0))
    }

    @Test
    fun chartScaleSwitchesToPercentOnlyWithoutAnFtp() {
        assertEquals(200.0, CalendarWorkoutFormatting.chartTarget(0.8, 250.0)!!, 1e-9)
        assertEquals(80.0, CalendarWorkoutFormatting.chartTarget(0.8, null)!!, 1e-9)
    }

    @Test
    fun zoneOpacityStopsMatchTheDesktop() {
        assertEquals(0.08, CalendarWorkoutFormatting.zoneOpacity(0.55), 1e-9)
        assertEquals(0.13, CalendarWorkoutFormatting.zoneOpacity(0.56), 1e-9)
        assertEquals(0.39, CalendarWorkoutFormatting.zoneOpacity(1.5), 1e-9)
        assertEquals(0.48, CalendarWorkoutFormatting.zoneOpacity(1.51), 1e-9)
    }

    @Test
    fun durationFormattingMatchesTheDesktop() {
        assertEquals("45s", CalendarWorkoutFormatting.duration(45.0))
        assertEquals("4 min", CalendarWorkoutFormatting.duration(240.0))
        assertEquals("1m 30s", CalendarWorkoutFormatting.duration(90.0))
        assertEquals("\u2014", CalendarWorkoutFormatting.duration(-1.0))
    }

    /**
     * The real publisher's profile decodes here too: iOS reads this vector in
     * its model tests; Android must not drift from the same file.
     */
    @Test
    fun thePublishedProfileVectorDecodesIntoBlocks() {
        val item = CloudItem.fromJson(TestVectors.parse("calendar_day_profile.json"))
        val day = (item.payload as CloudPayload.CalendarDay).value
        val workouts = day.workouts ?: emptyList()
        assertTrue("the vector carries workouts", workouts.isNotEmpty())
        val totalBlocks = workouts.sumOf { CalendarWorkoutProfileDecoder.blocks(it).size }
        assertTrue("the vector's profiles decode to blocks", totalBlocks > 0)
        val firstWithProfile = workouts.first { CalendarWorkoutProfileDecoder.blocks(it).isNotEmpty() }
        val blocks = CalendarWorkoutProfileDecoder.blocks(firstWithProfile)
        // Every kept block satisfies the publisher's own invariant.
        blocks.forEach { assertTrue(it.end - it.start == it.durationS) }
        assertFalse(blocks.any { it.kind.isEmpty() })
    }

    private fun assertNullish(value: Int?) {
        org.junit.Assert.assertNull(value)
    }
}
