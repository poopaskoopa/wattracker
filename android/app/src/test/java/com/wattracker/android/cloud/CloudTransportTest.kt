package com.wattracker.android.cloud

import java.io.ByteArrayInputStream
import java.io.IOException
import java.io.InputStream
import java.nio.charset.CharacterCodingException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The response-side parsing that the state machine trusts but the session's
 * scripted transport never exercises: `Retry-After` (a delta *and* an
 * HTTP-date) and the hard cap on a response body.
 */
class CloudTransportTest {

    // MARK: - Retry-After

    @Test
    fun retryAfterAsADeltaSecondsIsTakenAsSeconds() {
        val now = 1_700_000_000L
        assertEquals(120.0, parseRetryAfter("120", now)!!, 1e-9)
        assertEquals(0.0, parseRetryAfter("0", now)!!, 1e-9)
        assertEquals(45.0, parseRetryAfter("  45  ", now)!!, 1e-9)
        // A negative delta is clamped to now, never the past.
        assertEquals(0.0, parseRetryAfter("-5", now)!!, 1e-9)
    }

    @Test
    fun retryAfterAsAnHttpDateIsSecondsFromNow() {
        // Self-consistent: the delta is exactly the gap between `now` and the
        // date, whatever the absolute epoch is.
        val date = "Wed, 21 Oct 2026 07:28:00 GMT"
        val dateMillis = parseHttpDate(date)!!
        val nowSeconds = dateMillis / 1000 - 60
        assertEquals(60.0, parseRetryAfter(date, nowSeconds)!!, 0.001)
        // A date in the past is clamped to zero, not a negative backoff.
        assertEquals(0.0, parseRetryAfter(date, nowSeconds + 61)!!, 1e-9)
    }

    @Test
    fun retryAfterThatIsNeitherDeltaNorDateIsIgnored() {
        assertNull(parseRetryAfter("not-a-time", 1_700_000_000L))
        assertNull(parseRetryAfter("Wed, 32 Oct 2026 07:28:00 GMT", 1_700_000_000L))
    }

    @Test
    fun anUnparseableDateHeaderIsAbsenceNotAGuess() {
        assertNull(parseHttpDate(null))
        assertNull(parseHttpDate("garbage"))
        // The RFC 1123 shape the server actually sends parses.
        assertTrue(parseHttpDate("Wed, 21 Oct 2026 07:28:00 GMT")!! > 0L)
    }

    // MARK: - Response body cap

    @Test
    fun aBodyLargerThanTheCapIsTruncatedNotBufferedWhole() {
        // A stream that fills every chunk it is handed: the cap must still hold
        // the result to exactly the cap, not cap-plus-a-chunk.
        val cap = 16
        val body = readCapped(EndlessStream(), cap = cap)
        assertEquals(cap, body.size)
    }

    @Test
    fun aBodyWithinTheCapIsReturnedWhole() {
        val source = ByteArray(100) { it.toByte() }
        val body = readCapped(ByteArrayInputStream(source), cap = 4096)
        assertEquals(100, body.size)
        assertTrue(source.contentEquals(body))
    }

    @Test
    fun theResponseCapIsAFewMegabytes() {
        // The cap bounds bytes, then a String, then a JSON tree in memory at
        // once, and the largest real payload on this API is a 1500-point
        // stream page. Fifty MiB was an order of magnitude too loose; a few
        // is plenty.
        assertEquals(4 * 1024 * 1024, MAX_RESPONSE_BYTES)
    }

    // MARK: - Overall deadline

    @Test
    fun aResponseThatOutrunsTheDeadlineIsCutOff() {
        // A stream that keeps filling its buffer and a clock that marches
        // past the deadline on every read: the cap would hold the result, but
        // the deadline must end the request first, so a trickling server
        // cannot hold it open indefinitely. The cap is well past the point
        // where the deadline fires, so the deadline is what ends the read.
        var now = 1_000L
        val deadline = 5_000L
        val body = runCatching {
            readCapped(EndlessStream(), cap = 65536, deadline = deadline) { now += 2_000L; now }
        }
        assertTrue("expected the deadline to cut the read off", body.isFailure)
        assertTrue(body.exceptionOrNull() is IOException)
    }

    @Test
    fun aResponseDoneBeforeTheDeadlineIsWhole() {
        // The deadline must not truncate a body that finishes in time.
        val source = ByteArray(100) { it.toByte() }
        val body = readCapped(
            ByteArrayInputStream(source), cap = 4096, deadline = Long.MAX_VALUE,
        )
        assertEquals(100, body.size)
        assertTrue(source.contentEquals(body))
    }

    // MARK: - Strict UTF-8

    @Test
    fun aWellFormedUtf8BodyDecodes() {
        assertEquals("héllo — wörld", decodeUtf8("héllo — wörld".toByteArray(Charsets.UTF_8)))
        assertEquals("", decodeUtf8(ByteArray(0)))
    }

    @Test
    fun aMalformedUtf8BodyFailsRatherThanSubstituting() {
        // A lone 0xFF is not valid UTF-8. The lenient String(bytes, UTF_8)
        // would hand back a U+FFFD replacement character; the strict decoder
        // must throw so the client reads it as protocol drift.
        val malformed = byteArrayOf(0x61, 0xFF.toByte(), 0x62)
        val result = runCatching { decodeUtf8(malformed) }
        assertTrue(result.isFailure)
        assertTrue(result.exceptionOrNull() is CharacterCodingException)
    }

    @Test
    fun aTruncatedMultiByteSequenceFails() {
        // 'é' is two bytes; a body that carries only the first is malformed.
        val truncated = "aéb".toByteArray(Charsets.UTF_8).copyOfRange(0, 2)
        val result = runCatching { decodeUtf8(truncated) }
        assertTrue(result.isFailure)
    }
}

/** A stream that never ends: it always fills the buffer it is given. */
private class EndlessStream : InputStream() {
    override fun read(): Int = 0
    override fun read(b: ByteArray, off: Int, len: Int): Int {
        b.fill(0, off, len)
        return len
    }
}
