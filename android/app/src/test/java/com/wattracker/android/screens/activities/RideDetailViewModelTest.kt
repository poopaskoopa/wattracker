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
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
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
    ) : ReadSession {
        override suspend fun cached(route: CloudRoute): CloudSnapshot? =
            cachedSnapshot?.takeIf { it.route == route }

        override suspend fun load(route: CloudRoute): CloudSnapshot = throw NotImplementedError()
        override suspend fun activityDetail(activityId: Int): ActivityDetail = detailResult.getOrThrow()
        override suspend fun activityStreams(activityId: Int): ActivityStreams = streamsResult.getOrThrow()
        override suspend fun retryNowIfWaking() {}
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
}
