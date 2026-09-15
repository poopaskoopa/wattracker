package com.wattracker.android.cloud

/** Lowercase hex of a byte array. The server's regexes accept nothing else. */
fun ByteArray.toHex(): String = buildString(size * 2) {
    for (b in this@toHex) {
        val v = b.toInt() and 0xFF
        append(HEX[(v shr 4) and 0x0F])
        append(HEX[v and 0x0F])
    }
}

private const val HEX = "0123456789abcdef"

/**
 * Decode a lowercase-or-uppercase hex string to bytes.
 *
 * Returns null on any malformed input (odd length, non-hex digit) rather than
 * throwing partway: the callers (public-key parsing, signature parsing) want a
 * clean "unusable value" to report, not a decode exception that names an
 * offset.
 */
fun String.hexToBytes(): ByteArray? {
    if (length % 2 != 0) return null
    val out = ByteArray(length / 2)
    var i = 0
    while (i < length) {
        val hi = hexVal(this[i]) ?: return null
        val lo = hexVal(this[i + 1]) ?: return null
        out[i / 2] = ((hi shl 4) or lo).toByte()
        i += 2
    }
    return out
}

private fun hexVal(c: Char): Int? = when (c) {
    in '0'..'9' -> c - '0'
    in 'a'..'f' -> c - 'a' + 10
    in 'A'..'F' -> c - 'A' + 10
    else -> null
}
