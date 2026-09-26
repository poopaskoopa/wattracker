package com.wattracker.android.cloud

/**
 * Crockford Base32 pairing code normalization and formatting.
 *
 * Alphabet: 32 symbols with `I`, `L`, `O` and `U` removed.
 * Folds: `I` -> `1`, `L` -> `1`, `O` -> `0`.
 * Strips: `-`, spaces and tabs (matching server `_PAIRING_STRIP = {'-', ' ', '\t'}` in `wattracker/cloud/security.py`),
 * plus carriage returns and newlines (`\r`, `\n`) for clean copy-paste handling.
 * `U` is illegal and invalidates the code.
 */
object PairingCode {
    const val ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    const val SYMBOL_COUNT = 12
    const val GROUP_SIZE = 4
    private const val MAXIMUM_INPUT_LENGTH = 64

    private val FOLDS = mapOf('I' to '1', 'L' to '1', 'O' to '0')
    private val STRIPPED = setOf('-', ' ', '\t', '\r', '\n')

    /**
     * Return the canonical 12-symbol code, or null if the input is not valid.
     */
    fun normalized(value: String): String? {
        if (value.isEmpty() || value.length > MAXIMUM_INPUT_LENGTH) return null
        val symbols = StringBuilder(SYMBOL_COUNT)
        for (character in value.uppercase()) {
            if (character in STRIPPED) continue
            val folded = FOLDS[character] ?: character
            if (folded !in ALPHABET) return null
            symbols.append(folded)
        }
        return if (symbols.length == SYMBOL_COUNT) symbols.toString() else null
    }

    /**
     * Return the grouped display form (`XXXX-XXXX-XXXX`), or null if invalid.
     */
    fun grouped(value: String): String? {
        val canonical = normalized(value) ?: return null
        return canonical.chunked(GROUP_SIZE).joinToString("-")
    }
}
