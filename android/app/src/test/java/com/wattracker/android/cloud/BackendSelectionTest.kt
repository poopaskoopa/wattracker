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
        val durable = StoreHealth.Durable
        assertEquals(BackendObservation.Unknown, observeCloudBackend(sessionBuilt = false, paired = false, vaultHealth = durable))
        assertEquals(BackendObservation.Unpaired, observeCloudBackend(sessionBuilt = true, paired = false, vaultHealth = durable))
        assertEquals(BackendObservation.Paired, observeCloudBackend(sessionBuilt = true, paired = true, vaultHealth = durable))
    }

    @Test
    fun aVaultTheKeystoreRefusedIsNotALookAtTheCloudEither() {
        // The trap the review found: the session is built, `load()` answers null
        // because the in-memory store never opened the file, and a bare
        // `isPaired == false` reads that as a seen-unpaired cloud. It would then
        // delete an explicit Cloud override on a launch that looked at nothing.
        assertEquals(
            BackendObservation.Unknown,
            observeCloudBackend(sessionBuilt = true, paired = false, vaultHealth = StoreHealth.Unavailable),
        )
        assertTrue(
            "a Keystore-refused vault keeps an explicit Cloud choice",
            keepOverride(
                BackendSelection.Cloud,
                observeCloudBackend(sessionBuilt = true, paired = false, vaultHealth = StoreHealth.Unavailable),
                BackendObservation.Paired,
            ),
        )
    }

    @Test
    fun aCloudWhoseVaultCouldNotBeReadIsNotALookEither() {
        assertEquals(
            BackendObservation.Unknown,
            observeCloudBackend(sessionBuilt = true, paired = false, vaultHealth = StoreHealth.Unread),
        )
    }

    @Test
    fun anUnreadableCloudVaultIsASeenUnpairedCloud() {
        // The envelope opened and this installation cannot use it, so the
        // pairing behind it is gone. That is a look, and it says unpaired.
        assertEquals(
            BackendObservation.Unpaired,
            observeCloudBackend(sessionBuilt = true, paired = false, vaultHealth = StoreHealth.UnreadableVault),
        )
    }

    @Test
    fun aFailedReadIsNotALookAtTheDesktop() {
        // The second trap: one flaky read used to report `Durable`, which reads
        // as "looked and found nothing", and the override was dropped.
        assertEquals(BackendObservation.Unknown, observeLocalBackend(StoreHealth.Unread, paired = false))
        assertTrue(
            "one flaky read keeps an explicit Local choice",
            keepOverride(
                BackendSelection.Local,
                BackendObservation.Paired,
                observeLocalBackend(StoreHealth.Unread, paired = false),
            ),
        )
    }

    @Test
    fun anUnparsedCloudMemberLeavesTheCloudUnconcludedButNotTheDesktop() {
        // The review's finding D: one whole-vault flag would leave both
        // backends unconcluded when one member fails to parse. The pairing
        // that parsed is a real, served pairing.
        assertEquals(
            BackendObservation.Unknown,
            observeCloudBackend(sessionBuilt = true, paired = false, vaultHealth = StoreHealth.Durable, memberUnparsed = true),
        )
        assertEquals(
            BackendObservation.Paired,
            observeLocalBackend(StoreHealth.Durable, paired = true, memberUnparsed = false),
        )
    }

    @Test
    fun anUnparsedLocalMemberLeavesTheDesktopUnconcludedButNotTheCloud() {
        assertEquals(
            BackendObservation.Unknown,
            observeLocalBackend(StoreHealth.Durable, paired = false, memberUnparsed = true),
        )
        assertEquals(
            BackendObservation.Paired,
            observeCloudBackend(sessionBuilt = true, paired = true, vaultHealth = StoreHealth.Durable, memberUnparsed = false),
        )
    }

    @Test
    fun theLaunchObservationsReadThePairingFlagsBeforeTheHealth() {
        // The review's finding A: the health must answer from the vault read
        // the pairing flags trigger, not from before it. A plain
        // `vaultHealth` argument that reports Durable would read the desktop
        // as a seen-unpaired backend when the signer failed to build and
        // nothing had read the vault yet.
        val readOrder = mutableListOf<String>()
        val observations = launchObservations(
            cloudSessionBuilt = false,
            cloudPaired = { readOrder += "cloudPaired"; false },
            localPaired = { readOrder += "localPaired"; false },
            vaultHealth = { readOrder += "health"; StoreHealth.Durable },
            cloudMemberUnparsed = { readOrder += "cloudMemberUnparsed"; false },
            localMemberUnparsed = { readOrder += "localMemberUnparsed"; false },
        )
        assertEquals(
            listOf("cloudPaired", "localPaired", "health", "cloudMemberUnparsed", "localMemberUnparsed"),
            readOrder,
        )
        assertEquals(BackendObservation.Unknown, observations.cloud)
        assertEquals(BackendObservation.Unpaired, observations.local)
    }

    @Test
    fun theLaunchObservationsReportTheHealthReadAfterTheVault() {
        // The same trap from the other side: health is read after the paired
        // flags, so a health that reports Unread must be seen by the
        // observation, not by a stale read.
        val readOrder = mutableListOf<String>()
        val observations = launchObservations(
            cloudSessionBuilt = true,
            cloudPaired = { readOrder += "cloudPaired"; false },
            localPaired = { readOrder += "localPaired"; false },
            vaultHealth = { readOrder += "health"; StoreHealth.Unread },
            cloudMemberUnparsed = { false },
            localMemberUnparsed = { false },
        )
        assertTrue("the pairing flags were read before health", readOrder.indexOf("health") > readOrder.indexOf("localPaired"))
        assertEquals(BackendObservation.Unknown, observations.cloud)
        assertEquals(BackendObservation.Unknown, observations.local)
    }

    @Test
    fun aKeptOverrideSurvivesAndNothingIsDeleted() {
        val startup = decideStartupSelection(
            storedName = "Cloud",
            stored = BackendSelection.Cloud,
            cloud = BackendObservation.Paired,
            local = BackendObservation.Unpaired,
        )
        assertEquals(BackendSelection.Cloud, startup.selection)
        assertFalse("a kept preference must not be deleted", startup.deleteStoredPreference)
    }

    @Test
    fun aDroppedOverrideIsReplacedAndItsPreferenceDeleted() {
        val startup = decideStartupSelection(
            storedName = "Cloud",
            stored = BackendSelection.Cloud,
            cloud = BackendObservation.Unpaired,
            local = BackendObservation.Paired,
        )
        assertEquals(BackendSelection.Automatic, startup.selection)
        assertTrue("a dropped preference must be deleted, not re-read", startup.deleteStoredPreference)
    }

    @Test
    fun anUnrecognisedStoredValueIsDeleted() {
        // Hand-edited or from a build this one does not know. It parses to a null
        // `stored`, and leaving it would mean re-reading the same garbage every
        // launch.
        val startup = decideStartupSelection(
            storedName = "SomethingElse",
            stored = null,
            cloud = BackendObservation.Paired,
            local = BackendObservation.Paired,
        )
        assertEquals(BackendSelection.Automatic, startup.selection)
        assertTrue(startup.deleteStoredPreference)
    }

    @Test
    fun aFirstRunHasNothingToDelete() {
        val startup = decideStartupSelection(
            storedName = null,
            stored = null,
            cloud = BackendObservation.Unpaired,
            local = BackendObservation.Unpaired,
        )
        assertEquals(BackendSelection.Automatic, startup.selection)
        assertFalse("there is no preference to remove", startup.deleteStoredPreference)
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

    @Test
    fun theGuardSnapshotsTheGenerationAndTheSelectionTogether() {
        // The review's finding B: a resolve that reads the generation first
        // and the selection after can pass the check with the wrong selection.
        val guard = SelectionGuard()
        val (generation, _) = guard.snapshot()
        guard.set(BackendSelection.Cloud)
        val (_, after) = guard.snapshot()
        // A snapshot taken before the set must not resolve into the new
        // generation: the write is dropped, not applied.
        val applied = guard.dropIfStale(generation to BackendSelection.Local) { }
        assertFalse("a stale snapshot drops its resolve", applied)
        assertEquals(BackendSelection.Cloud, after)
    }

    @Test
    fun theGuardAppliesAResolveWhoseGenerationStillHolds() {
        val guard = SelectionGuard()
        val snapshot = guard.snapshot()
        var resolved: BackendSelection? = null
        val applied = guard.dropIfStale(snapshot) { resolved = it }
        assertTrue(applied)
        assertEquals(BackendSelection.Automatic, resolved)
    }

    @Test
    fun theGuardKeepsEverySetStaleProof() {
        // Each set moves the counter, so every snapshot taken before any
        // later set is stale -- the property an IO-thread refresh depends on.
        val guard = SelectionGuard()
        val first = guard.snapshot()
        guard.set(BackendSelection.Local)
        val second = guard.snapshot()
        guard.set(BackendSelection.Cloud)
        assertFalse(guard.dropIfStale(first, { }))
        assertFalse(guard.dropIfStale(second, { }))
    }
}
