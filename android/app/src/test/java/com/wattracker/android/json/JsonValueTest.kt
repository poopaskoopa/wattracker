package com.wattracker.android.json

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * The [JsonValue] parser's strictness.
 *
 * The parser must accept exactly the JSON the server emits and no more: a
 * body the server would never produce is protocol drift, surfaced as a
 * failure rather than a half-decoded value. These pin the boundaries the
 * review of PR #304 asked for -- leading zeros, raw control characters,
 * lone surrogates, and numbers that overflow [Double] -- and the one place
 * the parser is deliberately lenient (duplicate keys), which is a server
 * property, not a parsing accident.
 */
class JsonValueTest {

    private fun parseOrThrow(text: String): JsonValue = try {
        JsonValue.parse(text)
    } catch (e: JsonException) {
        fail("expected $text to parse, but it threw: ${e.message}")
        throw e
    }

    private fun expectRejected(text: String) {
        try {
            JsonValue.parse(text)
        } catch (_: JsonException) {
            return
        }
        fail("expected $text to be rejected, but it parsed")
    }

    // MARK: - Numbers

    @Test
    fun leadingZerosAreRejected() {
        expectRejected("01")
        expectRejected("010")
        expectRejected("-01")
        // The canonical forms still parse.
        assertEquals(1.0, parseOrThrow("1").asDouble()!!, 1e-12)
        assertEquals(10.0, parseOrThrow("10").asDouble()!!, 1e-12)
        assertEquals(0.0, parseOrThrow("0").asDouble()!!, 1e-12)
        assertEquals(0.5, parseOrThrow("0.5").asDouble()!!, 1e-12)
        assertEquals(100000.0, parseOrThrow("1e5").asDouble()!!, 1e-12)
        assertEquals(100000.0, parseOrThrow("1E+5").asDouble()!!, 1e-12)
    }

    @Test
    fun aNumberThatOverflowsDoubleIsRejected() {
        // toDoubleOrNull returns Infinity for these rather than null, so the
        // parser must refuse a non-finite result instead of emitting it.
        expectRejected("1e999")
        expectRejected("-1e999")
        expectRejected("1e400")
        // The largest finite value still parses.
        assertTrue(parseOrThrow("1.7976931348623157E308").asDouble()!! > 0.0)
    }

    @Test
    fun aTruncatedNumberIsRejected() {
        expectRejected("-")
        expectRejected(".")
        expectRejected("1.")
        expectRejected("1e")
        expectRejected("e5")
        expectRejected("1.2.3")
    }

    // MARK: - Strings

    @Test
    fun rawControlCharactersAreRejected() {
        // A raw U+0000-U+001F in the source is not a legal JSON string; it
        // must arrive escaped.
        expectRejected("\"a\u0001b\"")
        expectRejected("\"a\nb\"")
        expectRejected("\"a\rb\"")
        // The escaped forms still parse.
        assertEquals("a\nb", parseOrThrow("\"a\\nb\"").asString())
        assertEquals("a\rb", parseOrThrow("\"a\\rb\"").asString())
        assertEquals("a\u0001b", parseOrThrow("\"a\\u0001b\"").asString())
    }

    @Test
    fun aLoneSurrogateIsRejected() {
        // A high surrogate not followed by a low one, and a low one that
        // arrived out of the blue, are not characters.
        expectRejected("\"\\uD800\"")
        expectRejected("\"\\uDC00\"")
        expectRejected("\"a\\uD800b\"")
        // A real pair still parses to its code point.
        assertEquals("\uD83D\uDE00", parseOrThrow("\"\\uD83D\\uDE00\"").asString())
        assertEquals("\uD83D\uDE00", parseOrThrow("\"😀\"").asString())
    }

    @Test
    fun anInvalidEscapeIsRejected() {
        expectRejected("\"\\x41\"")
        expectRejected("\"\\z\"")
        expectRejected("\"\\u12\"")
        expectRejected("\"\\u-02d\"")
        expectRejected("\"\\")
    }

    // MARK: - Duplicate keys

    @Test
    fun duplicateKeysKeepTheLastLikeTheServerDoes() {
        // The server's json module keeps the last of a duplicate run; the
        // parser matches it rather than inventing a stricter rule the
        // publisher does not follow.
        val value = parseOrThrow("""{"a":1,"a":2}""")
        assertEquals(2.0, value.opt("a")!!.asDouble()!!, 1e-12)
    }

    // MARK: - Structure

    @Test
    fun trailingContentIsRejected() {
        expectRejected("1 2")
        expectRejected("{} []")
        expectRejected("\"a\" \"b\"")
        // The empty document is not legal JSON.
        expectRejected("")
        expectRejected("   ")
    }

    @Test
    fun aDeepNestingIsRejected() {
        // Far beyond anything the server emits; the recursion is bounded.
        val deep = "[".repeat(600) + "]".repeat(600)
        expectRejected(deep)
    }

    @Test
    fun roundTripPreservesAnUnknownKindByteForByte() {
        // Forward compatibility: a value with no model keeps and re-emits its
        // structure. Numbers normalize (6.0 -> 6), which the cache and the
        // interop tests compare on decoded values, never serialized bytes.
        val text = """{"id":"x","kind":"future_kind","revision":3,"data":{"nested":[1,2,3],"label":"a\"b\\c"}}"""
        val value = parseOrThrow(text)
        val reencoded = value.toJson()
        val reparsed = parseOrThrow(reencoded)
        assertEquals(value, reparsed)
        assertFalse(reencoded.contains("Infinity"))
    }
}
