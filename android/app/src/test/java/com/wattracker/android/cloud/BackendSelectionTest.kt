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

    @Test
    fun automaticIsAlwaysKept() {
        assertTrue(keepOverride(BackendSelection.Automatic, BackendObservation.Unpaired, BackendObservation.Unpaired))
        assertTrue(keepOverride(BackendSelection.Automatic, BackendObservation.Paired, BackendObservation.Paired))
    }

    @Test
    fun anOverrideForASeenUnpairedBackendIsDroppedAtStartup() {
        // The trap: a stored choice that no longer matches reality pins the app
        // to a pairing screen while the other backend works. iOS calls it out
        // in the comment at `:308` and clears it.
        assertFalse(keepOverride(BackendSelection.Cloud, BackendObservation.Unpaired, BackendObservation.Paired))
        assertFalse(keepOverride(BackendSelection.Local, BackendObservation.Paired, BackendObservation.Unpaired))
    }

    @Test
    fun anOverrideForAPairedBackendSurvives() {
        assertTrue(keepOverride(BackendSelection.Cloud, BackendObservation.Paired, BackendObservation.Unpaired))
        assertTrue(keepOverride(BackendSelection.Local, BackendObservation.Unpaired, BackendObservation.Paired))
    }

    @Test
    fun aBadLaunchDoesNotConsumeAChoiceAboutTheBackendItCouldNotSee() {
        // The Keystore handed over no signing key, so this launch never built a
        // cloud session and cannot say the cloud is unpaired -- only that it did
        // not look. A rider with a working desktop pairing and an explicit Cloud
        // override keeps the override, and gets it back on the next good launch.
        assertTrue(keepOverride(BackendSelection.Cloud, BackendObservation.Unknown, BackendObservation.Paired))
        // Symmetric: the Keystore would not open the vault, so the desktop
        // pairing was never read, and an explicit Local choice stands.
        assertTrue(keepOverride(BackendSelection.Local, BackendObservation.Paired, BackendObservation.Unknown))
        // Unknown is never evidence against the *other* backend's choice.
        assertTrue(keepOverride(BackendSelection.Cloud, BackendObservation.Paired, BackendObservation.Unknown))
    }

    @Test
    fun onlyASeenUnpairedBackendContradictsAnOverride() {
        assertTrue(BackendObservation.Unpaired.contradictsOverride)
        assertFalse(BackendObservation.Paired.contradictsOverride)
        assertFalse(BackendObservation.Unknown.contradictsOverride)
    }

    @Test
    fun aVaultTheKeystoreRefusedIsNotALookAtTheDesktop() {
        // The in-memory store stands in without ever opening the file on disk, so
        // a pairing may be sitting in it unread. That is not evidence against a
        // `Local` choice.
        assertEquals(BackendObservation.Unknown, observeLocalBackend(StoreHealth.Unavailable, paired = false))
    }

    @Test
    fun aVaultThatOpenedButCannotBeUsedIsASeenUnpairedDesktop() {
        // The file was read and the key behind it is gone, so the pairing went
        // with it. This side of the line the override is dropped, and Settings
        // already says why.
        val seen = observeLocalBackend(StoreHealth.UnreadableVault, paired = false)
        assertEquals(BackendObservation.Unpaired, seen)
        assertFalse(keepOverride(BackendSelection.Local, BackendObservation.Paired, seen))
    }

    @Test
    fun aDurableVaultReportsWhatItHolds() {
        assertEquals(BackendObservation.Paired, observeLocalBackend(StoreHealth.Durable, paired = true))
        assertEquals(BackendObservation.Unpaired, observeLocalBackend(StoreHealth.Durable, paired = false))
    }

    @Test
    fun aCloudThatWasNeverBuiltIsNotALookAtTheCloud() {
        assertEquals(BackendObservation.Unknown, observeCloudBackend(sessionBuilt = false, paired = false))
        assertEquals(BackendObservation.Unpaired, observeCloudBackend(sessionBuilt = true, paired = false))
        assertEquals(BackendObservation.Paired, observeCloudBackend(sessionBuilt = true, paired = true))
    }

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
