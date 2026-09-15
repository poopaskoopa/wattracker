package com.wattracker.android.json

/**
 * A JSON value the app can carry and re-emit without a model for it.
 *
 * This is the Kotlin twin of `ios/.../Cloud/JSONValue.swift`, and it exists for
 * the same two reasons that file documents:
 *
 * 1. **Forward compatibility.** The delta protocol is at-least-once but never
 *    at-least-twice: an object of a kind this build does not model must still
 *    be *kept*, byte-preserving, or a rider who updates the app finds a
 *    permanent hole where the objects the old build discarded used to be.
 *    Decoding an unknown kind into this and writing it back out is what makes
 *    an app update safe.
 * 2. **Sub-payloads the server does not fix.** A calendar day's `workouts`
 *    come straight out of the desktop's `db._plan_workout_row`, and an
 *    activity detail's `zones` is a summary block. Inventing a struct for
 *    either would be a second, weaker copy of a schema the desktop owns.
 *
 * It is a self-contained parser and serializer rather than `org.json` for a
 * concrete, load-bearing reason: `org.json` is an Android-framework class that
 * is *stubbed* in plain JVM unit tests, so the interop vector tests -- the ones
 * that read the shared JSON files under `tests/vectors` and prove this client
 * signs and decodes what the server expects -- could not run without an Android
 * runtime. A pure-Kotlin
 * value that behaves identically on the JVM and on device keeps those tests on
 * the same footing as the Python and Swift suites, and adds no dependency,
 * which is the epic's rule.
 */
sealed class JsonValue {
    object Null : JsonValue()
    data class Bool(val value: Boolean) : JsonValue()
    data class Number(val value: Double) : JsonValue()
    data class String(val value: kotlin.String) : JsonValue()
    data class Array(val values: List<JsonValue>) : JsonValue()
    data class Object(val fields: Map<kotlin.String, JsonValue>) : JsonValue()

    companion object {
        /** Parse a whole document into a [JsonValue]. */
        fun parse(text: kotlin.String): JsonValue = Parser(text).parseDocument()
    }
}

// MARK: - Convenience accessors
//
// The model layer decodes on top of these. Every reader is lenient about the
// JSON *type* it finds: a field the server omitted is null, a number arrives
// as a [JsonValue.Number] whether the source text was `6` or `6.5`, and a
// non-matching type reads as null rather than throwing. The decode rules the
// server relies on (a tombstone has no payload, an unknown kind still decodes)
// live in `CloudItem`; they build on this.

/** The field's value, or null when the object has no such key. */
fun JsonValue.opt(key: String): JsonValue? =
    (this as? JsonValue.Object)?.fields?.get(key)

/** A number field read as a [Double], or null when absent or not a number. */
fun JsonValue.optDouble(key: String): Double? =
    ((this as? JsonValue.Object)?.fields?.get(key) as? JsonValue.Number)?.value

fun JsonValue.optInt(key: String): Int? = optDouble(key)?.toInt()

fun JsonValue.optBoolean(key: String): Boolean? =
    ((this as? JsonValue.Object)?.fields?.get(key) as? JsonValue.Bool)?.value

fun JsonValue.optString(key: String): String? =
    ((this as? JsonValue.Object)?.fields?.get(key) as? JsonValue.String)?.value

/** A nested array read as [JsonValue], or null when absent or not an array. */
fun JsonValue.optArray(key: String): List<JsonValue>? =
    ((this as? JsonValue.Object)?.fields?.get(key) as? JsonValue.Array)?.values

/** This value read as a [Double], or null when it is not a number. */
fun JsonValue.asDouble(): Double? = (this as? JsonValue.Number)?.value

fun JsonValue.asBoolean(): Boolean? = (this as? JsonValue.Bool)?.value

fun JsonValue.asString(): String? = (this as? JsonValue.String)?.value

// MARK: - Serializer

/**
 * Serialize a [JsonValue] back to JSON text.
 *
 * Used to write the cache out to Room and to prove round-trips in tests. It is
 * deliberately canonical-but-simple: it is not a byte-for-byte replay of the
 * server's text (a `6.0` re-encodes as `6`, dropping the trailing `.0`), and
 * that is fine because both the tests and the cache compare *decoded values*,
 * never serialized bytes.
 */
fun JsonValue.toJson(): String = buildString {
    appendTo(this)
}

private fun JsonValue.appendTo(out: StringBuilder) {
    when (this) {
        is JsonValue.Null -> out.append("null")
        is JsonValue.Bool -> out.append(if (value) "true" else "false")
        is JsonValue.Number -> out.append(numberText(value))
        is JsonValue.String -> appendJsonString(value, out)
        is JsonValue.Array -> {
            out.append('[')
            values.forEachIndexed { index, value ->
                if (index > 0) out.append(',')
                value.appendTo(out)
            }
            out.append(']')
        }
        is JsonValue.Object -> {
            out.append('{')
            fields.entries.forEachIndexed { index, (key, value) ->
                if (index > 0) out.append(',')
                appendJsonString(key, out)
                out.append(':')
                value.appendTo(out)
            }
            out.append('}')
        }
    }
}

/** Render a [Double] the way JSON wants it: no trailing `.0` on whole numbers. */
private fun numberText(value: Double): String =
    if (value.isFinite() && value == value.toLong().toDouble()) {
        value.toLong().toString()
    } else {
        value.toString()
    }

private fun appendJsonString(value: String, out: StringBuilder) {
    out.append('"')
    for (c in value) {
        when (c) {
            '"' -> out.append("\\\"")
            '\\' -> out.append("\\\\")
            '\n' -> out.append("\\n")
            '\r' -> out.append("\\r")
            '\t' -> out.append("\\t")
            '\b' -> out.append("\\b")
            // Every other control character (U+0000-U+001F, the ones above
            // excepted) has no short escape, so it is written as \uXXXX.
            else -> {
                if (c < ' ') {
                    out.append("\\u").append("%04x".format(c.code))
                } else {
                    out.append(c)
                }
            }
        }
    }
    out.append('"')
}

// MARK: - Parser

/**
 * A small recursive-descent JSON parser.
 *
 * It accepts exactly the JSON the server emits and no more: object, array,
 * string, number, `true`, `false`, `null`, with the standard escapes. Numbers
 * are read through [toDouble], which accepts the fractional and
 * exponent forms the server's `round()` may or may not have produced -- the
 * reason the model layer types every numeric field as a `Double?`.
 *
 * It fails with [JsonException] on the first structural error; a malformed
 * body on this API is a `CloudClient` `malformedResponse`, so a thrown parse is
 * the correct behaviour, not something to swallow into a half-decoded object.
 */
class JsonException(message: String) : Exception(message)

/** Nesting ceiling, far above any real payload, that bounds the parse recursion. */
private const val MAX_JSON_DEPTH = 512

private class Parser(private val text: String) {
    private var pos = 0
    private var depth = 0

    fun parseDocument(): JsonValue {
        skipWhitespace()
        val value = parseValue()
        skipWhitespace()
        if (pos != text.length) {
            throw JsonException("trailing content at offset $pos")
        }
        return value
    }

    private fun parseValue(): JsonValue {
        skipWhitespace()
        if (pos >= text.length) throw JsonException("unexpected end of input")
        return when (val c = text[pos]) {
            '{' -> parseObject()
            '[' -> parseArray()
            '"' -> JsonValue.String(parseString())
            't', 'f' -> parseBoolean()
            'n' -> parseNull()
            else -> if (c in '0'..'9' || c == '-') parseNumber()
            else throw JsonException("unexpected character '$c' at offset $pos")
        }
    }

    private fun parseObject(): JsonValue {
        expect('{')
        depth += 1
        if (depth > MAX_JSON_DEPTH) throw JsonException("nesting exceeds the limit of $MAX_JSON_DEPTH")
        try {
            val fields = LinkedHashMap<String, JsonValue>()
            skipWhitespace()
            if (peek() == '}') {
                pos++
                return JsonValue.Object(fields)
            }
            while (true) {
                skipWhitespace()
                if (peek() != '"') throw JsonException("expected object key at offset $pos")
                val key = parseString()
                skipWhitespace()
                expect(':')
                val value = parseValue()
                // A duplicate key keeps the last value, exactly what the
                // server's json module does on decode; RFC 8259 only says
                // names *should* be unique, and the server never emits two.
                fields[key] = value
                skipWhitespace()
                when (val c = peek()) {
                    ',' -> pos++
                    '}' -> {
                        pos++
                        return JsonValue.Object(fields)
                    }
                    else -> throw JsonException("expected ',' or '}' at offset $pos (got '$c')")
                }
            }
        } finally {
            depth -= 1
        }
    }

    private fun parseArray(): JsonValue {
        expect('[')
        depth += 1
        if (depth > MAX_JSON_DEPTH) throw JsonException("nesting exceeds the limit of $MAX_JSON_DEPTH")
        try {
            val values = ArrayList<JsonValue>()
            skipWhitespace()
            if (peek() == ']') {
                pos++
                return JsonValue.Array(values)
            }
            while (true) {
                values.add(parseValue())
                skipWhitespace()
                when (val c = peek()) {
                    ',' -> pos++
                    ']' -> {
                        pos++
                        return JsonValue.Array(values)
                    }
                    else -> throw JsonException("expected ',' or ']' at offset $pos (got '$c')")
                }
            }
        } finally {
            depth -= 1
        }
    }

    private fun parseString(): String {
        expect('"')
        val out = StringBuilder()
        while (true) {
            if (pos >= text.length) throw JsonException("unterminated string")
            when (val c = text[pos++]) {
                '"' -> return out.toString()
                '\\' -> {
                    if (pos >= text.length) throw JsonException("unterminated escape")
                    when (val e = text[pos++]) {
                        '"' -> out.append('"')
                        '\\' -> out.append('\\')
                        '/' -> out.append('/')
                        'n' -> out.append('\n')
                        'r' -> out.append('\r')
                        't' -> out.append('\t')
                        'b' -> out.append('\b')
                        'f' -> out.append('\u000c')
                        'u' -> appendUnicodeEscape(out)
                        else -> throw JsonException("bad escape '\\$e'")
                    }
                }
                else -> {
                    // A raw control character (U+0000-U+001F) must be escaped
                    // in JSON text; the escape branch above is the only legal
                    // way one may appear in a string.
                    if (c < ' ') throw JsonException("raw control character at offset ${pos - 1}")
                    out.append(c)
                }
            }
        }
    }

    /**
     * A `\u` escape and, where the code is a high surrogate, the low
     * surrogate that must follow it. JSON text is Unicode, and a lone
     * surrogate is not a character: a string carrying one would re-serialize
     * as invalid UTF-8, corrupting the rider's data on the way to the cache.
     */
    private fun appendUnicodeEscape(out: StringBuilder) {
        val high = parseUnicodeEscape()
        when (high) {
            in 0xD800..0xDBFF -> {
                if (!text.startsWith("\\u", pos)) {
                    throw JsonException("unpaired high surrogate at offset $pos")
                }
                val pairStart = pos
                pos += 2
                val low = parseUnicodeEscape()
                if (low !in 0xDC00..0xDFFF) {
                    throw JsonException("high surrogate not followed by a low one at offset $pairStart")
                }
                out.appendCodePoint(((high - 0xD800) shl 10) or (low - 0xDC00) or 0x10000)
            }
            in 0xDC00..0xDFFF -> throw JsonException("unpaired low surrogate at offset $pos")
            else -> out.append(high.toChar())
        }
    }

    private fun parseUnicodeEscape(): Int {
        if (pos + 4 > text.length) {
            throw JsonException("truncated \\u escape")
        }
        val hex = text.substring(pos, pos + 4)
        // Strict JSON: all four must be ASCII hex digits, which rejects
        // signed forms like \u-02d.
        if (hex.any { it !in '0'..'9' && it !in 'a'..'f' && it !in 'A'..'F' }) {
            throw JsonException("bad \\u escape '$hex'")
        }
        val code = hex.toInt(16)
        pos += 4
        return code
    }

    private fun parseBoolean(): JsonValue {
        if (text.startsWith("true", pos)) {
            pos += 4
            return JsonValue.Bool(value = true)
        }
        if (text.startsWith("false", pos)) {
            pos += 5
            return JsonValue.Bool(value = false)
        }
        throw JsonException("invalid literal at offset $pos")
    }

    private fun parseNull(): JsonValue {
        if (text.startsWith("null", pos)) {
            pos += 4
            return JsonValue.Null
        }
        throw JsonException("invalid literal at offset $pos")
    }

    private fun parseNumber(): JsonValue {
        // Strict JSON number: an optional minus (no plus), an integer part
        // with no leading zeros, an optional fraction, and an optional
        // exponent. Anything else is an error, not a leniency -- the server
        // emits canonical JSON.
        val start = pos
        if (pos < text.length && text[pos] == '-') pos++
        requireDigit(start)
        val firstDigit = text[pos]
        pos++
        if (firstDigit == '0' && pos < text.length && text[pos] in '0'..'9') {
            throw JsonException("leading zeros in number at offset $start")
        }
        while (pos < text.length && text[pos] in '0'..'9') pos++
        if (pos < text.length && text[pos] == '.') {
            pos++
            requireDigit(start)
            while (pos < text.length && text[pos] in '0'..'9') pos++
        }
        if (pos < text.length && (text[pos] == 'e' || text[pos] == 'E')) {
            pos++
            if (pos < text.length && (text[pos] == '+' || text[pos] == '-')) pos++
            requireDigit(start)
            while (pos < text.length && text[pos] in '0'..'9') pos++
        }
        val literal = text.substring(start, pos)
        val value = literal.toDoubleOrNull()
            ?: throw JsonException("invalid number '$literal'")
        // toDoubleOrNull overflows to Infinity instead of failing, and a
        // non-finite value would serialize back out as invalid JSON. A number
        // past the Double range is protocol drift, not a value.
        if (!value.isFinite()) {
            throw JsonException("number out of range '$literal'")
        }
        return JsonValue.Number(value)
    }

    private fun requireDigit(start: Int) {
        if (pos >= text.length || text[pos] !in '0'..'9') {
            throw JsonException("invalid number at offset $start")
        }
    }

    private fun peek(): Char =
        if (pos < text.length) text[pos] else '\u0000'

    private fun expect(c: Char) {
        if (pos >= text.length || text[pos] != c) {
            throw JsonException("expected '$c' at offset $pos")
        }
        pos++
    }

    private fun skipWhitespace() {
        while (pos < text.length) {
            when (text[pos]) {
                ' ', '\t', '\n', '\r' -> pos++
                else -> break
            }
        }
    }
}
