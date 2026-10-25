package com.wattracker.android.screens

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeParseException

/**
 * The one reader of timestamps the server publishes, shared by the screens.
 *
 * Two things a `SimpleDateFormat` gets wrong, both invisible on clean test
 * fixtures:
 *
 * - A published timestamp can carry any 1 to 9 fractional seconds (Python
 *   writes microseconds), and a `.SSS` pattern reads `.123456` as 123,456 ms
 *   -- two minutes and three seconds off.
 * - `parse` stops at the first character it cannot read and calls the prefix a
 *   success, so a value in a form this build does not know is rendered or
 *   sorted by whatever happened to come before it instead of being rejected.
 *
 * What parses, in order: an ISO timestamp with an offset or `Z`, and an ISO
 * timestamp without one -- which the server writes from a UTC datetime, so it
 * is read as UTC. Nothing else is guessed at; callers decide what an
 * unparseable value is worth. iOS shows the raw string where Android renders
 * the instant, which `docs/mobile-parity.md` records as a deliberate
 * difference.
 */
internal object PublishedTime {

    /** [value] as an instant, or null when it is not a published timestamp. */
    fun parse(value: String?): Instant? {
        if (value.isNullOrBlank()) return null
        iso(value) { OffsetDateTime.parse(it) }?.let { return it.toInstant() }
        iso(value) { LocalDateTime.parse(it) }?.let { return it.toInstant(ZoneOffset.UTC) }
        return null
    }

    /**
     * [value] as UTC midnight of a date-only stamp -- what `load_point`
     * objects carry -- or null. Kept apart from [parse] on purpose: a bare
     * date is a day, not a ride's start, and a screen that renders midnight
     * as if it were a time is lying.
     */
    fun parseDay(value: String?): Instant? {
        if (value.isNullOrBlank()) return null
        return iso(value) { LocalDate.parse(it) }?.atStartOfDay(ZoneOffset.UTC)?.toInstant()
    }

    private fun <T> iso(value: String, read: (String) -> T): T? =
        try {
            read(value)
        } catch (_: DateTimeParseException) {
            null
        }
}
