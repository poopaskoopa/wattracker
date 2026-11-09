package com.wattracker.android.cloud

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The two pure rules the backend choice rests on: what a launch reads from,
 * and whether a stored override survives. iOS is the reference
 * (`Shell/SessionGate.swift:305-316`); `backend_selection_v1.json` (#483) is
 * what will pin them across both clients.
 */
class BackendSelectionTest {

    // MARK: - resolveBackend

    @Test
    fun automaticPrefersTheCloudWhenBothArePaired() {
        assertEquals(
            DataSource.Cloud,
            resolveBackend(BackendSelection.Automatic, cloudPaired = true, localPaired = true),
        )
    }

    @Test
    fun automaticFallsToTheDesktopWhenOnlyItIsPaired() {
        assertEquals(
            DataSource.Local,
            resolveBackend(BackendSelection.Automatic, cloudPaired = false, localPaired = true),
        )
    }

    @Test
    fun automaticLandsOnCloudWhenNothingIsPaired() {
        // The first-run case: the cloud is where the pairing flow lives, so a
        // rider with no backend at all opens on cloud pairing rather than on a
        // desktop form they may not have a server for.
        assertEquals(
            DataSource.Cloud,
            resolveBackend(BackendSelection.Automatic, cloudPaired = false, localPaired = false),
        )
    }

    @Test
    fun anOverrideIsNotReconsideredByPairingState() {
        // iOS: a persisted override is never changed by automatic selection,
        // only kept warm (`:377`). Reading from an unpaired backend is how the
        // pairing screen is reached at all -- and how Cancel gets something to
        // cancel out of.
        assertEquals(DataSource.Cloud, resolveBackend(BackendSelection.Cloud, cloudPaired = false, localPaired = true))
        assertEquals(DataSource.Local, resolveBackend(BackendSelection.Local, cloudPaired = true, localPaired = false))
    }

    @Test
    fun anOverrideNamesExactlyItsOwnBackend() {
        assertEquals(DataSource.Cloud, resolveBackend(BackendSelection.Cloud, cloudPaired = true, localPaired = true))
        assertEquals(DataSource.Local, resolveBackend(BackendSelection.Local, cloudPaired = true, localPaired = true))
    }

    // MARK: - keepOverride

    @Test
    fun automaticIsAlwaysKept() {
        assertTrue(keepOverride(BackendSelection.Automatic, cloudPaired = false, localPaired = false))
        assertTrue(keepOverride(BackendSelection.Automatic, cloudPaired = true, localPaired = true))
    }

    @Test
    fun anOverrideForAnUnpairedBackendIsDroppedAtStartup() {
        // The trap: a stored choice that no longer matches reality pins the app
        // to a pairing screen while the other backend works. iOS calls it out
        // in the comment at `:308` and clears it.
        assertFalse(keepOverride(BackendSelection.Cloud, cloudPaired = false, localPaired = true))
        assertFalse(keepOverride(BackendSelection.Local, cloudPaired = true, localPaired = false))
    }

    @Test
    fun anOverrideForAPairedBackendSurvives() {
        assertTrue(keepOverride(BackendSelection.Cloud, cloudPaired = true, localPaired = false))
        assertTrue(keepOverride(BackendSelection.Local, cloudPaired = false, localPaired = true))
    }

    // MARK: - the rest of the type

    @Test
    fun onlyAnOverrideIsPersisted() {
        assertFalse(BackendSelection.Automatic.isOverride)
        assertTrue(BackendSelection.Cloud.isOverride)
        assertTrue(BackendSelection.Local.isOverride)
    }

    @Test
    fun aBackendMapsToTheChoiceThatNamesIt() {
        assertEquals(BackendSelection.Cloud, BackendSelection.of(DataSource.Cloud))
        assertEquals(BackendSelection.Local, BackendSelection.of(DataSource.Local))
    }
}
