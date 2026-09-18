package com.wattracker.android.cloud

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The identity gate behind the generation-stamped cache writes, and the
 * process-local rule that keeps a restart from freezing the cache.
 */
class GenerationGateTest {

    @Test
    fun aFreshGateAcceptsTheFreshIdentity() {
        // A fresh process starts at generation 0 against a gate that has
        // never committed: the first write must land.
        val gate = GenerationGate()
        assertTrue(gate.accepts(0))
    }

    @Test
    fun aCommittedIdentityRefusesOnlyOlderOnes() {
        val gate = GenerationGate()
        gate.commit(1) // a removal or re-pairing committed under identity 1
        assertTrue(gate.accepts(1))
        assertFalse(gate.accepts(0)) // the superseded read's write is refused
    }

    @Test
    fun aRestartIsAFreshGateNotAPersistedOne() {
        // The PR #304 blocker: the gate must not outlive the process. A
        // restarted session starts a fresh gate at 0; the prior process's
        // committed identity cannot veto it.
        val priorProcess = GenerationGate()
        priorProcess.commit(1)
        val restarted = GenerationGate()
        assertTrue(restarted.accepts(0))
    }
}
