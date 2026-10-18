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
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
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
        var detailHandler: (suspend (Int) -> ActivityDetail)? = null,
        var streamsHandler: (suspend (Int) -> ActivityStreams)? = null,
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
    fun aPullWhileALoadIsInFlightLeavesTheFlagsToTheNewLoad() = runTest(StandardTestDispatcher()) {
        // A standard dispatcher on purpose: the unconfined one the other tests
        // use runs each launch straight through, so it cannot show a cancelled
        // load clearing the flags of the load that replaced it. A pull
        // restarts a load of the *same* ride, which is exactly the case the
        // ride id cannot tell apart.
        val gate = CompletableDeferred<Unit>()
        val session = FakeReadSession(
            detailHandler = {
                gate.await()
                detail
            },
            streamsHandler = {
                gate.await()
                streamsWithPower()
            },
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)

        vm.load(17)
        runCurrent()
        assertTrue("the first load is in flight", vm.uiState.value.isLoading)
        assertFalse(vm.uiState.value.isRefreshing)

        vm.pullToRefresh()
        runCurrent()

        val during = vm.uiState.value
        assertTrue("the pull's indicator survives the load it cancelled", during.isRefreshing)
        assertTrue("and the new load is still the loading one", during.isLoading)

        gate.complete(Unit)
        // `runCurrent` is what dispatches the two resumed reads; on a standard
        // dispatcher the awaits do not resume inside `advanceUntilIdle` alone.
        runCurrent()
        advanceUntilIdle()

        val after = vm.uiState.value
        assertFalse("the pull's indicator is cleared when its load finishes", after.isRefreshing)
        assertFalse("and so is the spinner", after.isLoading)
        assertNotNull(after.detail)
        assertNotNull(after.streams)
    }

    @Test
    fun aPullKeepsTheRideOnScreenWhileItReReads() = runTest(testDispatcher) {
        val gate = CompletableDeferred<Unit>()
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.success(streamsWithPower()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)
        assertNotNull(vm.uiState.value.detail)
        assertNotNull(vm.uiState.value.streams)

        // A pull whose reads then suspend. The header, the summary and the
        // chart stay painted -- collapsing them to "Unknown date" and a
        // spinner is not what a re-read of the same ride should cost.
        session.detailHandler = {
            gate.await()
            detail
        }
        session.streamsHandler = {
            gate.await()
            streamsWithPower()
        }
        vm.pullToRefresh()

        val during = vm.uiState.value
        assertNotNull("the detail stays put", during.detail)
        assertNotNull("and so does the chart", during.streams)
        assertEquals("2026-01-02T18:04:00", during.shownSummary?.startTime)
        assertTrue(during.isRefreshing)

        gate.complete(Unit)
        advanceUntilIdle()
        assertFalse(vm.uiState.value.isRefreshing)
        assertNotNull(vm.uiState.value.streams)
    }

    @Test
    fun switchingRidesDropsTheRideJustLeft() = runTest(testDispatcher) {
        val gate = CompletableDeferred<Unit>()
        val session = FakeReadSession(
            detailResult = Result.success(detail),
            streamsResult = Result.success(streamsWithPower()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)
        assertNotNull(vm.uiState.value.streams)

        // Keeping a re-read's data is not the same as keeping another ride's:
        // the 1,500-point stream of the ride just left must not sit under a
        // ride that is still loading.
        session.detailHandler = {
            gate.await()
            detail
        }
        session.streamsHandler = {
            gate.await()
            streamsWithPower()
        }
        vm.load(18)

        val during = vm.uiState.value
        assertNull(during.detail)
        assertNull(during.streams)
        assertNull(during.shownSummary)
        assertTrue(during.isLoading)
    }

    @Test
    fun aStreamsAnswerAfterTheCredentialWentAwayIsNotWritten() = runTest(testDispatcher) {
        // The two reads fail independently, so the streams can answer after
        // the detail reported a device that no longer exists. That answer
        // belongs to a pane that has since been cleared to the removed state.
        val session = FakeReadSession(
            detailResult = Result.failure(CloudSession.Failure.DeviceRemoved()),
            streamsResult = Result.success(streamsWithPower()),
        )
        val vm = RideDetailViewModel(session, coroutineScope = backgroundScope)
        vm.load(17)

        val state = vm.uiState.value
        assertEquals(CloudSession.DeviceState.removed, state.deviceState)
        assertFalse(state.isPaired)
        assertNull("the cleared pane keeps nothing from the late read", state.streams)
        assertNull(state.detail)
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
