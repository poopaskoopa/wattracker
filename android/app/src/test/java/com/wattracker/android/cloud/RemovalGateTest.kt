package com.wattracker.android.cloud

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The in-memory [RemovalGate], the reference the durable one follows: the
 * flag is read without being consumed, and it is cleared only by the caller,
 * after the retried wipe succeeds. A gate that cleared itself on read could
 * not be retried after a process death in the middle of the wipe.
 */
class RemovalGateTest {

    @Test
    fun readingTheFlagDoesNotConsumeIt() {
        val gate = InMemoryRemovalGate()
        assertFalse(gate.isPending())
        gate.markPending()
        assertTrue(gate.isPending())
        assertTrue(gate.isPending()) // a second start sees it too
    }

    @Test
    fun clearOnlyHappensWhenTheCallerSaysSo() {
        val gate = InMemoryRemovalGate()
        gate.markPending()
        // The caller does this, and only after the wipe succeeded.
        gate.clearPending()
        assertFalse(gate.isPending())
    }
}
