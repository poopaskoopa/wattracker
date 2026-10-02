package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optBoolean
import com.wattracker.android.json.optInt
import com.wattracker.android.json.optString
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Kotlin half of the shared pairing-code behaviour vectors.
 *
 * This reads `tests/vectors/pairing_code_v1.json` -- generated from the
 * server's `normalize_pairing_code` / `format_pairing_code` by
 * `scripts/generate_behaviour_vectors.py`, and read by the Python suite
 * (`tests/test_behaviour_vectors.py`) and the Swift suite
 * (`BehaviourVectorTests`) too. The server is the truth. The one leniency a
 * client may have is stripping `\r` / `\n` (cases marked `client_may_accept`),
 * and only because this client sends the server the normalized code rather
 * than the raw input.
 */
class PairingCodeVectorTest {

    private val vectors: JsonValue = TestVectors.parse("pairing_code_v1.json")

    private fun cases(): List<JsonValue> =
        vectors.optArray("cases") ?: throw AssertionError("vector file is missing cases")

    @Test
    fun constantsMatchTheServer() {
        assertEquals(vectors.optString("alphabet"), PairingCode.ALPHABET)
        assertEquals(vectors.optInt("symbol_count"), PairingCode.SYMBOL_COUNT)
        assertEquals(vectors.optInt("group_size"), PairingCode.GROUP_SIZE)
    }

    @Test
    fun everyPairingCodeVectorMatches() {
        val cases = cases()
        assertFalse(cases.isEmpty())
        for (entry in cases) {
            val name = entry.optString("name")!!
            val input = entry.optString("input")!!
            val normalized = PairingCode.normalized(input)
            val grouped = PairingCode.grouped(input)
            if (entry.optBoolean("client_may_accept") == true) {
                // Either refuse like the server, or accept as exactly the
                // stripped code -- never anything else.
                val lenient = entry.optString("client_normalized")!!
                val lenientGrouped = entry.optString("client_grouped")!!
                assertTrue("$name: $normalized", normalized == null || normalized == lenient)
                assertTrue("$name: $grouped", grouped == null || grouped == lenientGrouped)
            } else {
                // optString is null for JSON null, which is the expected
                // "not a code" answer.
                assertEquals(name, entry.optString("normalized"), normalized)
                assertEquals(name, entry.optString("grouped"), grouped)
            }
        }
    }
}
