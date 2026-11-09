package com.wattracker.android.cloud

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The read seam under the two shapes the cloud can arrive in: built, and not
 * built at all. The second one is what #475 A1's `Unusable` phase needs to be
 * able to tell apart from "not paired", and it must not cost the rider the
 * desktop backend they did pair.
 */
class ReadModelTest {

    private fun desktop() = LocalClient(
        credentialsProvider = { LocalCredentials(serverUrl = "https://desktop.example", token = "tok") },
    )

    private val noCloud = ReadModel(cloud = null, local = desktop(), source = { DataSource.Cloud })

    @Test
    fun aCloudThatCouldNotBeBuiltReadsAsUnpaired() {
        assertEquals(CloudSession.DeviceState.unpaired, noCloud.deviceState)
        assertFalse(noCloud.isPaired)
        assertNull(noCloud.lastSuccess)
        assertNull(runBlocking { noCloud.cached(CloudRoute.Dashboard) })
    }

    @Test
    fun theAbsentCloudClaimsNoIdentityARealBackendCould() {
        // Both backends start their identity at 0. If the seam returned 0 too,
        // a caller comparing identity across a backend change could read
        // "unchanged" when the backend behind it was replaced outright.
        assertEquals(-1, noCloud.identity)
        assertNotEquals(noCloud.identity, desktop().identity)
    }

    @Test
    fun aCloudThatCouldNotBeBuiltRefusesAReadLikeAnUnpairedOne() {
        // Screens already branch on this failure for an unpaired cloud, so a
        // missing session must not invent a second path through every screen.
        assertThrows(CloudSession.Failure.NotPaired::class.java) {
            runBlocking { noCloud.load(CloudRoute.Dashboard) }
        }
    }

    @Test
    fun aMissingCloudNeverClaimsToBeWaking() {
        // Pull to refresh lifts a cold-start gate. There is no client here to
        // hold one, so the answer is a flat no.
        assertFalse(noCloud.mayBeWaking)
        runBlocking { noCloud.retryNowIfWaking() }
    }

    @Test
    fun theMissingCloudLeavesThePairedDesktopServing() {
        val paired = ReadModel(cloud = null, local = desktop(), source = { DataSource.Local })
        assertTrue(paired.isPaired)
        assertEquals(CloudSession.DeviceState.paired, paired.deviceState)
        assertEquals(DataSource.Local, paired.activeDataSource)
    }

    @Test
    fun theSeamFollowsTheResolvedSourceNotWhatWasBuilt() {
        // One model, both backends: the resolution decides per read, so a
        // launch that could only build one backend still flips between them.
        var source = DataSource.Cloud
        val model = ReadModel(cloud = null, local = desktop(), source = { source })
        assertFalse(model.isPaired)
        source = DataSource.Local
        assertTrue(model.isPaired)
    }
}
