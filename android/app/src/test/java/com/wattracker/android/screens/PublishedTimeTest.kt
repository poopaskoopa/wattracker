package com.wattracker.android.screens

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The screens' shared timestamp reader.
 *
 * Every case here is a shape the server has actually published, or a shape a
 * lenient parser used to accept and get wrong. `RideDisplayTest` and
 * `DashboardDataTest` check what their own screens make of these instants;
 * this file pins the reading itself so the two screens cannot drift apart
 * again.
 */
class PublishedTimeTest {

    private val utcMidnightMay10: Instant =
        LocalDate.of(2024, 5, 10).atStartOfDay(ZoneOffset.UTC).toInstant()

    private val sixPmFour: Instant =
        LocalDateTime.of(2024, 5, 10, 18, 4, 0).toInstant(ZoneOffset.UTC)

    @Test
    fun anOffsetStampIsReadAtItsOffset() {
        assertEquals("Z", sixPmFour, PublishedTime.parse("2024-05-10T18:04:00Z"))
        assertEquals(
            "the same instant written from a +02:00 rider",
            sixPmFour,
            PublishedTime.parse("2024-05-10T20:04:00+02:00"),
        )
    }

    @Test
    fun aNaiveStampIsReadAsUtc() {
        // The server writes naive strings from UTC datetimes.
        assertEquals("naive is UTC", sixPmFour, PublishedTime.parse("2024-05-10T18:04:00"))
    }

    @Test
    fun aFractionIsReadAtItsTrueScale() {
        // The bug this replaces: a `.SSS` pattern reads `.123456` as 123,456
        // ms, which is two minutes and three seconds past the instant the
        // rider actually started. A fraction is a fraction of a second, at
        // whatever width it is published.
        assertEquals("one digit", sixPmFour.plusNanos(100_000_000), PublishedTime.parse("2024-05-10T18:04:00.1Z"))
        assertEquals("three digits", sixPmFour.plusNanos(123_000_000), PublishedTime.parse("2024-05-10T18:04:00.123Z"))
        assertEquals(
            "microseconds are microseconds, not milliseconds",
            sixPmFour.plusNanos(123_456_000),
            PublishedTime.parse("2024-05-10T18:04:00.123456Z"),
        )
        assertEquals(
            "nine digits",
            sixPmFour.plusNanos(999_999_999),
            PublishedTime.parse("2024-05-10T18:04:00.999999999Z"),
        )
        // The shape the screens render from: the old formatter read
        // `.123456` as 123,456 ms, so the stamp stopped being its own second.
        assertEquals(
            "still inside the second it names",
            sixPmFour,
            PublishedTime.parse("2024-05-10T18:04:00.123456Z")!!.truncatedTo(ChronoUnit.SECONDS),
        )
    }

    @Test
    fun aDayIsADayAndNotATimestamp() {
        assertEquals("a load point's day", utcMidnightMay10, PublishedTime.parseDay("2024-05-10"))
        // A bare date is a day, not a ride's start: the timestamp reader
        // refuses it rather than inventing midnight for a screen to render.
        assertNull("no time to read", PublishedTime.parse("2024-05-10"))
        // And the day reader refuses a timestamp it would have to truncate.
        assertNull("not a bare day", PublishedTime.parseDay("2024-05-10T18:04:00Z"))
    }

    @Test
    fun nothingIsGuessedAt() {
        // Each of the first three used to parse, to a wrong instant, under a
        // lenient `SimpleDateFormat` that stopped at the first character it
        // could not read and called the prefix a success.
        assertNull("an offset without its colon", PublishedTime.parse("2024-05-10T18:04:00+0200"))
        assertNull("a readable prefix of garbage", PublishedTime.parse("2024-05-10T18:04:00Garbage"))
        assertNull("a different order of the same numbers", PublishedTime.parse("10/05/2024 18:04"))
        assertNull("a date that cannot exist", PublishedTime.parse("2024-13-45"))
        assertNull("empty", PublishedTime.parse(""))
        assertNull("blank", PublishedTime.parse("   "))
        assertNull("absent", PublishedTime.parse(null))
        assertNull("absent day", PublishedTime.parseDay(null))
    }
}
