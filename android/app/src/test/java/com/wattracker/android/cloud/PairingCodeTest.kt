package com.wattracker.android.cloud

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairingCodeTest {

    @Test
    fun normalizesValidCodeWithHyphensAndSpaces() {
        val input = " 0123 - 4567 - 89AB "
        val normalized = PairingCode.normalized(input)
        assertEquals("0123456789AB", normalized)
        assertEquals("0123-4567-89AB", PairingCode.grouped(input))
    }

    @Test
    fun normalizesTabsAndNewlines() {
        val input = "0123\t4567\r\n89AB"
        val normalized = PairingCode.normalized(input)
        assertEquals("0123456789AB", normalized)
    }

    @Test
    fun foldsLetterLookalikes() {
        val input = "i123-l567-o9ab"
        val normalized = PairingCode.normalized(input)
        assertEquals("1123156709AB", normalized)
        assertEquals("1123-1567-09AB", PairingCode.grouped(input))
    }

    @Test
    fun rejectsIllegalLetterU() {
        val input = "0123-4567-89AU"
        assertNull(PairingCode.normalized(input))
        assertNull(PairingCode.grouped(input))
    }

    @Test
    fun rejectsWrongSymbolCount() {
        assertNull(PairingCode.normalized("0123-4567"))
        assertNull(PairingCode.normalized("0123-4567-89ABC"))
    }

    @Test
    fun rejectsNonBase32Characters() {
        assertNull(PairingCode.normalized("0123-4567-89A!"))
    }

    @Test
    fun rejectsEmptyInputAndExceedingLengthLimit() {
        assertNull(PairingCode.normalized(""))
        assertNull(PairingCode.normalized("a".repeat(65)))
    }
}
