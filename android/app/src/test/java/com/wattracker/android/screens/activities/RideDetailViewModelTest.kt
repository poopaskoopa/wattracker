package com.wattracker.android.screens.activities

import com.wattracker.android.cloud.ActivityDetail
import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.LocalClientException
import com.wattracker.android.cloud.ReadSession
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class RideDetailViewModelTest {

    private val testDispatcher = UnconfinedTestDispatcher()

    @Before
    fun setUp() {
        kotlinx.coroutines.Dispatchers.setMain(testDispatcher)
    }

    @After
    fun tearDown() {
        kotlinx.coroutines.Dispatchers.resetMain()
    }

    private class FakeReadSession(
        override var deviceState: CloudSession.DeviceState = CloudSession.DeviceState.paired,
        override var isPaired: Boolean = true,
        override var identity: Int = 1,
        override var lastSuccess: Long? = 1000L,
        override var mayBeWaking: Boolean = false,
        private val cachedSnapshot: CloudSnapshot? = null,
        var detailResult: Result<ActivityDetail> = Result.failure(NotImplementedError()),
        var streamsResult: Result<ActivityStreams> = Result.failure(NotImplementedError()),
        /**
         * Handlers win over the canned results where a test needs a read that
         * suspends. Null means "use `detailResult`/`streamsResult`", read as
         * properties so a test can swap the canned result mid-flight.
         */
        val detailHandler: (suspend (Int) -> ActivityDetail)? = null,
        val streamsHandler: (suspend (Int) -> ActivityStreams)? = null,
        val retryNowIfWakingHandler: suspend () -> Unit = {},
    ) : ReadSession {
        /** How often each read went out. */
        var detailStarts = 0
        var streamsStarts = 0
        var wakingGateLifts = 0

        override suspend fun cached(route: CloudRoute): CloudSnapshot? =
            cachedSnapshot?.takeIf { it.route == route }

        override suspend fun load(route: CloudRoute): CloudSnapshot = throw NotImplementedError()

        override suspend fun activityDetail(activityId: Int): ActivityDetail {
            detailStarts += 1
            return detailHandler?.invoke(activityId) ?: detailResult.getOrThrow()
        }

        override suspend fun activityStreams(activityId: Int): ActivityStreams {
            streamsStarts += 1
            return streamsHandler?.invoke(activityId) ?: streamsResult.getOrThrow()
        }

        override suspend fun retryNowIfWaking() {
            wakingGateLifts += 1
            retryNowIfWakingHandler()
        }
    }

    private val detail = ActivityDetail(
        id = 17.0,
        startTime = "2026-01-02T18:04:00",
        durationS = 3600.0,
        distanceM = 32100.5,
        avgPower = 211.0,
        avgHr = 142.0,
        np = 225.0,
        intensityFactor = 0.9,
        tss = 81.0,
        rpe = 6.0,
        weightKg = 72.0,
        weightSource = "settings",
        weightDate = "2026-01-01",
        zones = null,
    )

    private fun streamsWithPower() = ActivityStreams(
        streams = ActivityStreams.Channels(
            time = listOf(0.0, 1.0),
            power = listOf(180.0, 205.0),
            heartrate = null,
            cadence = null,
            altitude = null,
        ),
    )

    private fun cachedSnapshotWithRide() = CloudSnapshot(
        route = CloudRoute.Activities,
        revision = 1,
        items = listOf(
            CloudItem(
                id = "activity-17",
                kind = CloudKind.Activity,
                revision = 1,
                payload = CloudPayload.Activity(
                    ActivitySummary(
                        id = 17.0,
                        startTime = "2026-01-02T18:04:00",
                        durationS = 3600.0,
                        distanceM = null,
                        avgPower = null,
                        avgHr = null,
                        np = null,
                        intensityFactor = null,
                        tss = null,
                        rpe = null,
                    ),
                ),
            ),
        ),
        source = CloudSnapshot.Source.cache,
        asOf = 1000L,
    )

    @Test
    fun loadsDetailAndStreams() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.success(streamsWithPower()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertFalse(state.isLoading)
        assertNotNull(state.detail)
        assertNotNull(state.streams)
        assertFalse(state.noStreams)
        assertNull(state.detailError)
        assertNull(state.streamsError)
        assertNotNull(state.shownSummary)
        assertEquals(3600.0, state.shownSummary!!.durationS!!, 1e-9)
    }

    @Test
    fun aCloudStreams404IsNoRecordedStreams() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.failure(CloudSession.Failure.Server(404)),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertTrue("a ride without plottable streams says so, not 'failed'", state.noStreams)
        assertNull(state.streams)
        assertNull(state.streamsError)
    }

    @Test
    fun aLocalStreams404IsNoRecordedStreams() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.failure(LocalClientException.Http(status = 404, path = "/api/activity/17", retryAfter = null)),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertTrue(state.noStreams)
        assertNull(state.streamsError)
    }

    @Test
    fun anEmptyStreamsPayloadIsNoRecordedStreams() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.success(
                ActivityStreams(streams = ActivityStreams.Channels(null, null, null, null, null)),
            ),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertTrue(state.noStreams)
        assertNull(state.streams)
        assertNull(state.streamsError)
    }

    @Test
    fun anOrdinaryStreamsFailureShowsTheError() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.failure(CloudSession.Failure.Offline()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertFalse(state.noStreams)
        assertNotNull(state.streamsError)
        assertNotNull(state.detail)
    }

    @Test
    fun aDetailFailureDoesNotHideTheChart() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.failure(CloudSession.Failure.Offline()),
            streamsResult = Result.success(streamsWithPower()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertNotNull(state.detailError)
        assertNotNull(state.streams)
    }

    @Test
    fun theSummaryComesFromTheCachedList() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshot = cachedSnapshotWithRide(),
            detailResult = Result.success(detail),
            streamsResult = Result.success(streamsWithPower()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        assertEquals("2026-01-02T18:04:00", vm.uiState.value.shownSummary?.startTime)
    }

    @Test
    fun unpairedShowsNoDataAndNoReads() = runTest(testDispatcher) {
        val session = FakeReadSession(
            deviceState = CloudSession.DeviceState.unpaired,
            isPaired = false,
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertFalse(state.isLoading)
        assertNull(state.detail)
        assertNull(state.streams)
    }

    @Test
    fun loadResetsWakingStateAndReloads() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.failure(CloudSession.Failure.Waking()),
            streamsResult = Result.failure(CloudSession.Failure.Waking()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)
        assertTrue(vm.uiState.value.isWaking)

        // On reload after cloud wakes up
        session.detailResult = Result.success(detail)
        session.streamsResult = Result.success(streamsWithPower())
        vm.reload()

        val state = vm.uiState.value
        assertFalse(state.isWaking)
        assertNotNull(state.detail)
        assertNotNull(state.streams)
    }

    @Test
    fun releaseClearsRideData() = runTest(testDispatcher) {
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.success(streamsWithPower()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)
        assertNotNull(vm.uiState.value.detail)

        vm.release()
        assertNull(vm.uiState.value.detail)
        assertNull(vm.uiState.value.streams)
        assertNull(vm.uiState.value.summary)
    }

    @Test
    fun supersedingALoadCancelsTheReadsForTheRideJustLeft() = runTest(testDispatcher) {
        // A gate neither read can pass: what the test can see is whether the
        // ride just left stopped reading. Reads launched as siblings of the
        // load rather than children of it keep running here, and a rider
        // clicking through the two-pane list pushes the cloud into 429s.
        val gate = CompletableDeferred<Unit>()
        var detailCancels = 0
        var streamsCancels = 0
        val session = FakeReadSession(
            detailHandler = {
                try {
                    gate.await()
                } catch (e: CancellationException) {
                    detailCancels += 1
                    throw e
                }
                detail
            },
            streamsHandler = {
                try {
                    gate.await()
                } catch (e: CancellationException) {
                    streamsCancels += 1
                    throw e
                }
                streamsWithPower()
            },
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)
        assertEquals("both reads went out", 1, session.detailStarts)
        assertEquals(1, session.streamsStarts)

        vm.load(18)
        advanceUntilIdle()

        assertEquals("the new ride's reads went out", 2, session.detailStarts)
        assertEquals(2, session.streamsStarts)
        assertEquals(
            "and the old ride's went with the load that started them",
            1,
            detailCancels,
        )
        assertEquals(1, streamsCancels)

        gate.complete(Unit)
        advanceUntilIdle()
    }

    @Test
    fun theDetailAndZonesPaintWithoutWaitingForTheStreams() = runTest(testDispatcher) {
        // The stream payload is the large one (1,500 points a channel).
        // Waiting for it before painting the detail is what iOS stopped doing
        // when it started assigning `detail` as soon as it landed.
        val streamsGate = CompletableDeferred<Unit>()
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsHandler = {
                streamsGate.await()
                streamsWithPower()
            },
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val painted = vm.uiState.value
        assertNotNull("the detail paints while the streams are still in flight", painted.detail)
        assertNotNull(painted.shownSummary)
        assertNull(painted.streams)
        assertTrue(painted.isLoading)

        streamsGate.complete(Unit)
        advanceUntilIdle()
        assertNotNull(vm.uiState.value.streams)
        assertFalse(vm.uiState.value.isLoading)
    }

    @Test
    fun aPullLiftsTheWakingGateAndReRunsBothReads() = runTest(testDispatcher) {
        val session = FakeReadSession(
            mayBeWaking = true,
            detailResult = Result.failure(CloudSession.Failure.Waking()),
            streamsResult = Result.failure(CloudSession.Failure.Waking()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)
        assertTrue(vm.uiState.value.isWaking)
        assertEquals("an automatic load never lifts the gate", 0, session.wakingGateLifts)

        vm.pullToRefresh()
        advanceUntilIdle()

        assertEquals("the rider's pull lifted the waking gate", 1, session.wakingGateLifts)
        assertEquals("both reads re-ran", 2, session.detailStarts)
        assertEquals(2, session.streamsStarts)
        assertFalse("the pull's indicator clears with the load", vm.uiState.value.isRefreshing)
        assertTrue("still waking: the server has not answered yet", vm.uiState.value.isWaking)
    }

    @Test
    fun aPullBeforeAnythingIsLoadedReadsNothing() = runTest(testDispatcher) {
        val session = FakeReadSession(mayBeWaking = true)
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.pullToRefresh()
        advanceUntilIdle()
        assertEquals(0, session.detailStarts)
        assertEquals(0, session.wakingGateLifts)
    }
}
